"""Amazon OpenSearch Serverless (AOSS) vector store adapter.

Implements :class:`aquifer.core.interfaces.VectorStore` against an AOSS *vector search*
collection using the k-NN query API. SigV4 request signing uses the ``aoss`` service name.

Design constraints (from the AOSS "supported operations" reference):

- **No custom document ids.** Classic vector collections reject ``_id`` on index/create, so
  chunks are written with system-generated ids. Our logical ``chunk_id`` is a stored field.
- **No ``_delete_by_query``.** Deletes are a search for the matching ``_id``s followed by bulk
  ``delete`` actions.
- **Eventual consistency.** Writes become searchable after the refresh interval (up to 60s on
  Classic collections), so replacement is *write the new generation, then delete older
  generations* — never delete-then-write. Readers prefer the newest generation if two are
  briefly visible.

The boto3/opensearch-py clients are created lazily and can be injected for testing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from aquifer.core.config import VectorStoreSettings, get_settings
from aquifer.core.errors import TransientError, log_event
from aquifer.core.interfaces import VectorStore
from aquifer.core.models import IndexedState, SearchResult
from aquifer.vectorstore.schema import (
    DATE_FIELDS,
    FILTERABLE_FIELDS,
    INTEGER_FIELDS,
    KEYWORD_FIELDS,
    RETRYABLE_STATUSES,
    chunk_rank,
    chunk_to_source,
    generation_rank,
    source_to_result,
    validate_filters,
)

logger = logging.getLogger(__name__)

# Kept for backwards compatibility with code that imported the private name.
_FILTERABLE_FIELDS = FILTERABLE_FIELDS

_PAGE = 500
_MAX_SCAN = 10_000  # from+size ceiling for a plain search
_SOURCE_EXCLUDES = {"excludes": ["vector"]}


def _is_index_not_found(exc: BaseException) -> bool:
    return getattr(exc, "status_code", None) == 404 and "index_not_found" in str(
        getattr(exc, "error", "") or exc
    )


class AossVectorStore(VectorStore):
    def __init__(
        self,
        settings: VectorStoreSettings | None = None,
        dimensions: int | None = None,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings().vector_store
        self._sleep = sleep
        if dimensions is None:
            dimensions = get_settings().embedding.dimensions
        self._dimensions = dimensions
        self._client = client

    @property
    def client(self) -> Any:
        """Lazily construct a SigV4-signed OpenSearch client for the AOSS endpoint."""
        if self._client is None:
            import boto3
            from opensearchpy import (
                AWSV4SignerAuth,
                OpenSearch,
                RequestsHttpConnection,
            )

            credentials = boto3.Session().get_credentials()
            auth = AWSV4SignerAuth(credentials, self._settings.region, self._settings.service)
            host = self._settings.endpoint.replace("https://", "").replace("http://", "")
            self._client = OpenSearch(
                hosts=[{"host": host, "port": 443}],
                http_auth=auth,
                use_ssl=True,
                verify_certs=True,
                connection_class=RequestsHttpConnection,
                pool_maxsize=20,
                timeout=self._settings.timeout_s,
                max_retries=self._settings.max_retries,
                retry_on_timeout=True,
                retry_on_status=(429, 502, 503, 504),
            )
        return self._client

    @property
    def _index(self) -> str:
        return self._settings.index

    # --- schema ----------------------------------------------------------

    def _properties(self) -> dict:
        props: dict = {
            "vector": {
                "type": "knn_vector",
                "dimension": self._dimensions,
                "method": {
                    "name": "hnsw",
                    "engine": self._settings.engine,
                    "space_type": self._settings.space_type,
                },
            },
            "text": {"type": "text"},
            "title": {"type": "text"},
            # Neutral semantic metadata. Flattened keyword arrays make entities and
            # relationships efficiently filterable; the full objects ride in _source only.
            "summary": {"type": "text"},
            "entities": {"type": "object", "enabled": False},
            "relationships": {"type": "object", "enabled": False},
            "source_metadata": {"type": "object", "enabled": False},
        }
        props.update({f: {"type": "keyword"} for f in KEYWORD_FIELDS})
        props.update({f: {"type": "date"} for f in DATE_FIELDS})
        props.update({f: {"type": "integer"} for f in INTEGER_FIELDS})
        return props

    def _index_body(self) -> dict:
        return {
            "settings": {"index": {"knn": True}},
            "mappings": {"properties": self._properties()},
        }

    def ensure_index(self) -> None:
        """Create the index, or add any fields introduced since it was created (idempotent)."""
        if not self.client.indices.exists(index=self._index):
            try:
                self.client.indices.create(index=self._index, body=self._index_body())
                log_event(logger, "index_created", index=self._index)
            except Exception as exc:
                # Concurrent cold starts race to create it; losing that race is fine.
                if "resource_already_exists" not in str(getattr(exc, "error", "") or exc):
                    raise
            self._wait_until_visible()
            return
        # Adding new fields to an existing mapping is allowed; changing existing ones is not, so
        # only the non-vector fields are sent and a conflict is logged rather than fatal.
        # ``entities``/``relationships`` are excluded: older indexes mapped them as dynamic
        # objects and toggling ``enabled`` is a rejected mapping change.
        props = {
            k: v
            for k, v in self._properties().items()
            if k not in {"vector", "entities", "relationships"}
        }
        try:
            self.client.indices.put_mapping(index=self._index, body={"properties": props})
        except Exception as exc:  # noqa: BLE001 - mapping drift must not stop ingestion
            log_event(
                logger,
                "index_mapping_update_failed",
                level=logging.WARNING,
                index=self._index,
                error=type(exc).__name__,
            )

    def _wait_until_visible(self, timeout_s: float = 90.0) -> None:
        """A new AOSS index can answer ``index_not_found`` for a short while after creation.

        Writing during that window could auto-create an index with a dynamic mapping (no vector
        field), so wait until it is visible before returning.
        """
        deadline = time.monotonic() + timeout_s
        while not self.client.indices.exists(index=self._index):
            if time.monotonic() > deadline:
                raise TransientError(f"index {self._index} not visible after {timeout_s}s")
            self._sleep(2.0)

    # --- writes ----------------------------------------------------------

    @staticmethod
    def _to_source(chunk) -> dict:  # noqa: ANN001 - kept for backwards compatibility
        return chunk_to_source(chunk)

    def _bulk(self, actions: list[dict], operation: str) -> None:
        if not actions:
            return
        response = self.client.bulk(body=actions)
        if response.get("errors"):
            failed = [
                item for item in response.get("items", [])
                if next(iter(item.values())).get("error")
            ]
            statuses = {next(iter(i.values())).get("status") for i in failed}
            if operation == "delete":
                # A 404 on delete means "already gone" — that is success for us.
                failed = [i for i in failed if next(iter(i.values())).get("status") != 404]
                if not failed:
                    return
            message = f"AOSS bulk {operation} had {len(failed)} failed item(s)"
            if statuses & {429, 500, 502, 503, 504}:
                raise TransientError(message)
            raise RuntimeError(message)

    def upsert(self, chunks: Sequence) -> None:
        if not chunks:
            return
        actions: list[dict] = []
        for chunk in chunks:
            # No "_id": Classic vector collections reject custom document ids.
            actions.append({"index": {"_index": self._index}})
            actions.append(chunk_to_source(chunk))
        self._bulk(actions, "index")

    def _search(self, body: dict) -> dict:
        try:
            return self.client.search(index=self._index, body=body)
        except Exception as exc:
            # Before the first worker run the index does not exist yet; for every reader
            # (discovery's purge, the MCP tools) that simply means "nothing indexed".
            if _is_index_not_found(exc):
                return {"hits": {"hits": []}, "aggregations": {}}
            raise

    def _scan(self, query: dict, fields: list[str]) -> list[dict]:
        """Return ``{"_id", **fields}`` for every hit of ``query`` (up to 10k)."""
        hits: list[dict] = []
        start = 0
        while start < _MAX_SCAN:
            body = {
                "from": start,
                "size": _PAGE,
                "query": query,
                "_source": {"includes": fields},
            }
            page = self._search(body).get("hits", {}).get("hits", [])
            for hit in page:
                hits.append({"_id": hit.get("_id"), **(hit.get("_source") or {})})
            if len(page) < _PAGE:
                break
            start += _PAGE
        return hits

    def _delete_ids(self, ids: list[str]) -> None:
        for i in range(0, len(ids), _PAGE):
            batch = ids[i : i + _PAGE]
            self._bulk([{"delete": {"_index": self._index, "_id": _id}} for _id in batch],
                       "delete")

    def replace_document(self, document_id: str, chunks: Sequence) -> None:
        """Write the new generation, then delete older generations of the same document."""
        if not chunks:
            self.delete_by_document(document_id)
            return
        self.upsert(chunks)
        ours = chunk_rank(chunks[0])
        existing = self._scan(
            {"term": {"document_id": document_id}},
            ["generation", "updated_at", "revision_committed_at", "indexed_at"],
        )
        # Only lower-ranked generations: never a newer version written concurrently, and two
        # writers of the same version converge on one survivor.
        own = chunks[0].generation
        self._delete_ids([h["_id"] for h in existing
                          if h.get("generation") != own and generation_rank(h) < ours])

    def delete_by_document(self, document_id: str) -> None:
        self.delete_by_filter({"document_id": document_id})

    def delete_documents(self, document_ids: Sequence[str]) -> None:
        if document_ids:
            self.delete_by_filter({"document_id": list(document_ids)})

    def delete_by_filter(self, filters: dict) -> int:
        query = self._build_filter(filters)
        if query is None:
            raise ValueError("delete_by_filter requires at least one filter")
        ids = [h["_id"] for h in self._scan(query, ["document_id"])]
        self._delete_ids(ids)
        return len(ids)

    # --- reads -----------------------------------------------------------

    def _build_filter(self, filters: dict | None) -> dict | None:
        filters = validate_filters(filters)
        if not filters:
            return None
        clauses = []
        for field, value in filters.items():
            if isinstance(value, list):
                clauses.append({"terms": {field: value}})
            else:
                clauses.append({"term": {field: value}})
        return {"bool": {"filter": clauses}}

    def query(
        self, vector: Sequence[float], k: int, filters: dict | None = None
    ) -> list[SearchResult]:
        knn: dict = {"vector": {"vector": list(vector), "k": k}}
        filter_query = self._build_filter(filters)
        if filter_query is not None:
            knn["vector"]["filter"] = filter_query

        body = {"size": k, "query": {"knn": knn}, "_source": _SOURCE_EXCLUDES}
        return self._hits_to_results(self._search(body))

    def search_by_metadata(self, filters: dict, k: int) -> list[SearchResult]:
        filter_query = self._build_filter(filters)
        query = filter_query if filter_query is not None else {"match_all": {}}
        body = {
            "size": k,
            "query": query,
            "_source": _SOURCE_EXCLUDES,
            # Deterministic order: most recently updated sources first.
            "sort": [
                {"updated_at": {"order": "desc", "unmapped_type": "date", "missing": "_last"}},
                {"chunk_index": {"order": "asc", "unmapped_type": "integer"}},
            ],
        }
        return self._hits_to_results(self._search(body))

    def get_by_document(self, document_id: str) -> list[dict]:
        body = {
            "size": 1000,
            "query": {"term": {"document_id": document_id}},
            "sort": [{"chunk_index": {"order": "asc"}}],
            "_source": _SOURCE_EXCLUDES,
        }
        response = self._search(body)
        return [hit.get("_source", {}) for hit in response.get("hits", {}).get("hits", [])]

    def get_indexed_state(self, document_ids: Sequence[str]) -> dict[str, IndexedState]:
        if not document_ids:
            return {}
        hits = self._scan(
            {"terms": {"document_id": list(document_ids)}},
            ["document_id", "content_hash", "updated_at", "extraction_status", "indexed_at",
             "extraction_attempts", "extraction_error", "next_retry_at"],
        )
        newest: dict[str, dict] = {}
        for hit in hits:
            doc_id = hit.get("document_id")
            if not doc_id:
                continue
            current = newest.get(doc_id)
            if current is None or (hit.get("indexed_at") or "") > (current.get("indexed_at") or ""):
                newest[doc_id] = hit
        return {
            doc_id: IndexedState(
                document_id=doc_id,
                content_hash=h.get("content_hash"),
                updated_at=h.get("updated_at"),
                extraction_status=h.get("extraction_status"),
                extraction_attempts=h.get("extraction_attempts") or 0,
                extraction_error=h.get("extraction_error"),
                next_retry_at=h.get("next_retry_at"),
            )
            for doc_id, h in newest.items()
        }

    def get_document_vectors(self, document_id: str) -> dict[str, list[float]]:
        body = {
            "size": 1000,
            "query": {"term": {"document_id": document_id}},
            "_source": {"includes": ["text", "vector"]},
        }
        hits = self._search(body).get("hits", {}).get("hits", [])
        return {h["_source"]["text"]: h["_source"]["vector"] for h in hits
                if (h.get("_source") or {}).get("vector")}

    def documents_due_for_retry(self, now, limit: int = 200) -> list[str] | None:  # noqa: ANN001
        """Documents in retry_pending (or legacy partial) whose next_retry_at has passed, plus
        legacy "failed" documents without an error code (indexed before retry tracking)."""
        now_iso = now.isoformat()
        query = {"bool": {"should": [
            {"bool": {"filter": [
                {"terms": {"extraction_status": list(RETRYABLE_STATUSES)}},
                {"bool": {"should": [
                    {"range": {"next_retry_at": {"lte": now_iso}}},
                    {"bool": {"must_not": [{"exists": {"field": "next_retry_at"}}]}},
                ], "minimum_should_match": 1}},
            ]}},
            {"bool": {
                "filter": [{"term": {"extraction_status": "failed"}}],
                "must_not": [{"exists": {"field": "extraction_error"}}],
            }},
        ], "minimum_should_match": 1}}
        body = {"size": 0, "query": query,
                "aggs": {"ids": {"terms": {"field": "document_id", "size": limit}}}}
        aggs = self._search(body).get("aggregations", {})
        return [b["key"] for b in aggs.get("ids", {}).get("buckets", [])]

    def _aggregate(self, filters: dict | None, aggs: dict) -> dict:
        query = self._build_filter(filters) or {"match_all": {}}
        body = {"size": 0, "query": query, "aggs": aggs}
        return self._search(body).get("aggregations", {})

    def list_document_ids(self, filters: dict, limit: int = 10_000) -> set[str] | None:
        aggs = self._aggregate(filters, {"ids": {"terms": {"field": "document_id", "size": limit}}})
        return {b["key"] for b in aggs.get("ids", {}).get("buckets", [])}

    def facet(
        self, field: str, filters: dict | None = None, size: int = 100
    ) -> list[tuple[str, int]] | None:
        if field not in FILTERABLE_FIELDS:
            raise ValueError(f"Unsupported facet field: {field!r}")
        aggs = self._aggregate(
            filters,
            {
                "values": {
                    "terms": {"field": field, "size": size},
                    "aggs": {"docs": {"cardinality": {"field": "document_id"}}},
                }
            },
        )
        return [
            (b["key"], int(b.get("docs", {}).get("value") or b.get("doc_count", 0)))
            for b in aggs.get("values", {}).get("buckets", [])
        ]

    def max_value(self, field: str, filters: dict | None = None):  # noqa: ANN201
        if field not in DATE_FIELDS:
            raise ValueError(f"Unsupported max field: {field!r}")
        aggs = self._aggregate(filters, {"m": {"max": {"field": field}}})
        m = aggs.get("m", {})
        return m.get("value_as_string") or m.get("value")

    @staticmethod
    def _hits_to_results(response: dict) -> list[SearchResult]:
        return [
            source_to_result(hit.get("_source", {}), score=hit.get("_score") or 0.0,
                             hit_id=hit.get("_id", ""))
            for hit in response.get("hits", {}).get("hits", [])
        ]
