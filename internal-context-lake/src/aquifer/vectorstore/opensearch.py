"""Amazon OpenSearch Serverless (AOSS) vector store adapter.

Implements :class:`aquifer.core.interfaces.VectorStore` against an AOSS *vector search*
collection using the k-NN query API. SigV4 request signing uses the ``aoss`` service name.

Design notes:
- Chunk ids are deterministic, so ``upsert`` indexes with an explicit ``_id`` and is idempotent.
- ``delete_by_document`` removes a document's prior chunks before re-ingestion of an edit, so
  stale chunks never linger when a GitHub item's text changes.
- The boto3/opensearch-py clients are created lazily and can be injected for testing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aquifer.core.config import VectorStoreSettings, get_settings
from aquifer.core.interfaces import VectorStore
from aquifer.core.models import SearchResult

# Document fields that are safe to expose as query filters. Includes the flattened semantic
# fields so agents can retrieve by objective entity/relationship without any interpretation.
_FILTERABLE_FIELDS = {
    "document_id",
    "source_type",
    "kind",
    "repo",
    "author",
    "labels",
    "entity_names",
    "entity_types",
    "topics",
    "relationship_targets",
    "relationship_types",
}


class AossVectorStore(VectorStore):
    def __init__(
        self,
        settings: VectorStoreSettings | None = None,
        dimensions: int | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings().vector_store
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
            )
        return self._client

    # --- schema ----------------------------------------------------------

    def _index_body(self) -> dict:
        return {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
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
                    "document_id": {"type": "keyword"},
                    "chunk_index": {"type": "integer"},
                    "source_type": {"type": "keyword"},
                    "kind": {"type": "keyword"},
                    "repo": {"type": "keyword"},
                    "title": {"type": "text"},
                    "url": {"type": "keyword"},
                    "author": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "labels": {"type": "keyword"},
                    # Neutral semantic metadata. Flattened keyword arrays make entities and
                    # relationships efficiently filterable; the full objects ride in _source.
                    "summary": {"type": "text"},
                    "entities": {"type": "object"},
                    "entity_names": {"type": "keyword"},
                    "entity_types": {"type": "keyword"},
                    "topics": {"type": "keyword"},
                    "relationships": {"type": "object"},
                    "relationship_targets": {"type": "keyword"},
                    "relationship_types": {"type": "keyword"},
                }
            },
        }

    def ensure_index(self) -> None:
        if not self.client.indices.exists(index=self._settings.index):
            self.client.indices.create(index=self._settings.index, body=self._index_body())

    # --- writes ----------------------------------------------------------

    @staticmethod
    def _to_source(chunk) -> dict:  # noqa: ANN001 - Chunk, avoids import cycle in signature
        def _iso(dt):
            return dt.isoformat() if dt is not None else None

        return {
            "vector": chunk.embedding,
            "text": chunk.text,
            "document_id": chunk.document_id,
            "chunk_index": chunk.index,
            "source_type": str(chunk.source_type),
            "kind": str(chunk.kind),
            "repo": chunk.repo,
            "title": chunk.title,
            "url": chunk.url,
            "author": chunk.author,
            "created_at": _iso(chunk.created_at),
            "updated_at": _iso(chunk.updated_at),
            "labels": chunk.labels,
            # Neutral semantic metadata: full structured objects plus flattened keyword arrays.
            "summary": chunk.summary,
            "entities": [e.model_dump() for e in chunk.entities],
            "entity_names": [e.name for e in chunk.entities],
            "entity_types": [e.type for e in chunk.entities],
            "topics": chunk.topics,
            "relationships": [r.model_dump() for r in chunk.relationships],
            "relationship_targets": [r.target for r in chunk.relationships],
            "relationship_types": [r.type for r in chunk.relationships],
        }

    def upsert(self, chunks: Sequence) -> None:
        if not chunks:
            return
        actions: list[dict] = []
        for chunk in chunks:
            actions.append({"index": {"_index": self._settings.index, "_id": chunk.chunk_id}})
            actions.append(self._to_source(chunk))
        response = self.client.bulk(body=actions)
        if response.get("errors"):
            failed = [
                item for item in response.get("items", [])
                if next(iter(item.values())).get("error")
            ]
            raise RuntimeError(f"AOSS bulk upsert had {len(failed)} failed item(s)")

    def delete_by_document(self, document_id: str) -> None:
        self.client.delete_by_query(
            index=self._settings.index,
            body={"query": {"term": {"document_id": document_id}}},
        )

    # --- reads -----------------------------------------------------------

    def _build_filter(self, filters: dict | None) -> dict | None:
        if not filters:
            return None
        clauses = []
        for field, value in filters.items():
            if field not in _FILTERABLE_FIELDS:
                raise ValueError(f"Unsupported filter field: {field!r}")
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

        body = {"size": k, "query": {"knn": knn}}
        response = self.client.search(index=self._settings.index, body=body)
        return self._hits_to_results(response)

    def search_by_metadata(self, filters: dict, k: int) -> list[SearchResult]:
        filter_query = self._build_filter(filters)
        query = filter_query if filter_query is not None else {"match_all": {}}
        body = {"size": k, "query": query}
        response = self.client.search(index=self._settings.index, body=body)
        return self._hits_to_results(response)

    def get_by_document(self, document_id: str) -> list[dict]:
        body = {
            "size": 1000,
            "query": {"term": {"document_id": document_id}},
            "sort": [{"chunk_index": {"order": "asc"}}],
        }
        response = self.client.search(index=self._settings.index, body=body)
        return [hit.get("_source", {}) for hit in response.get("hits", {}).get("hits", [])]

    @staticmethod
    def _hits_to_results(response: dict) -> list[SearchResult]:
        results: list[SearchResult] = []
        for hit in response.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            results.append(
                SearchResult(
                    chunk_id=hit.get("_id", ""),
                    document_id=src.get("document_id", ""),
                    score=hit.get("_score") or 0.0,
                    text=src.get("text", ""),
                    title=src.get("title", ""),
                    url=src.get("url", ""),
                    source_type=src.get("source_type"),
                    kind=src.get("kind"),
                    repo=src.get("repo"),
                    summary=src.get("summary", ""),
                    entities=src.get("entities", []),
                    topics=src.get("topics", []),
                    relationships=src.get("relationships", []),
                )
            )
        return results
