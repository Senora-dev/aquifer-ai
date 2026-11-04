"""Integrator jobs on the single-server job queue, and the daily scheduler.

- ``mcp_sync`` (one per source run): connect, check the plan against the tools the server
  offers *now* (a tool that disappeared or changed safety class stops the run with a clear
  error), then enqueue the first page of every stream.
- ``mcp_page``: one page of one stream (see :mod:`aquifer.integrator.sync`); enqueues the next
  page, or finishes the stream (high-water mark, deletion reconciliation) and, when every
  stream is done, the run.
- ``mcp_authorize``: the interactive OAuth step. The CLI requests it; this job publishes the
  authorization URL, waits (up to 10 minutes) for the browser redirect delivered to
  ``/oauth/callback``, stores the tokens and checks the connection.

Failures are classified (see :mod:`aquifer.integrator.client`): rate limits postpone the page;
an authorization failure marks the source ``needs_reauth``; a tool error fails that stream for
this run; anything transient is retried by the queue with backoff and ends in the dead letters
if it persists.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aquifer.core.config import Settings
from aquifer.core.errors import log_event
from aquifer.integrator.client import (
    IntegratorError,
    SourceRateLimited,
    SourceUnavailable,
    classify_failure,
    connect,
    sanitize,
)
from aquifer.integrator.credentials import NeedsAuthorization
from aquifer.integrator.models import Source
from aquifer.integrator.plan import validate_plan
from aquifer.integrator.registry import Registry
from aquifer.integrator.sync import reconcile_stream, run_page

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _registry(path: str) -> Registry:
    return Registry(path)


def registry_for(settings: Settings) -> Registry:
    return _registry(str(Path(settings.node.data_dir) / "sources.db"))


def credentials_ref(settings: Settings, source: str, kind: str) -> str:
    """Where a source's credential is stored (``kind``: token, oauth, client-secret)."""
    prefix = settings.integrator.credentials_prefix
    if prefix:
        return f"{prefix.rstrip('/')}/{source}/{kind}"
    return f"file:{Path(settings.node.data_dir) / 'credentials' / f'{source}-{kind}.json'}"


def redirect_uri(settings: Settings) -> str:
    return settings.integrator.public_url.rstrip("/") + "/oauth/callback"


_pipelines: dict[str, object] = {}


def pipeline_for(settings: Settings):  # noqa: ANN201
    """The shared ingestion pipeline (same extraction, chunking, embedding and store)."""
    key = settings.vector_store.sqlite_path + settings.vector_store.memory_path
    if key not in _pipelines:
        from aquifer.core.hooks import get_pipeline
        from aquifer.ingestion.factory import build_embedder, build_vector_store
        from aquifer.ingestion.pipeline import IngestionPipeline

        _pipelines[key] = IngestionPipeline(
            connector=None, embedder=build_embedder(settings),
            vector_store=build_vector_store(settings), interceptors=get_pipeline(),
            chunk_size=settings.ingestion.chunk_size,
            chunk_overlap=settings.ingestion.chunk_overlap)
    return _pipelines[key]


# --- sources visible to Aquifer's MCP tools ----------------------------------------------

def sources_provider(settings: Settings):  # noqa: ANN201
    """For ``ContextTools.extra_sources``: enabled sources, cached for a few seconds."""
    cache: dict = {"at": 0.0, "value": []}

    def provider() -> list[dict]:
        if time.monotonic() - cache["at"] > 5:
            reg = registry_for(settings)
            cache["value"] = [
                {"scope": s.scope, "name": s.name, "provider": s.provider,
                 "mcp_server": s.url, "state": s.state,
                 "last_successful_sync": reg.last_success(s.name),
                 "sync_coverage": [st.coverage() for st in s.plan.streams]}
                for s in reg.all() if s.enabled]
            cache["at"] = time.monotonic()
        return cache["value"]

    return provider


# --- helpers ------------------------------------------------------------------------------

def _set_state(reg: Registry, name: str, state: str) -> None:
    source = reg.get(name)
    if source is not None and source.state != state:
        source.state = state  # type: ignore[assignment]
        reg.save(source)


def _maybe_finish(reg: Registry, source: Source, run_id: str) -> None:
    run = reg.run(run_id)
    if run is None or run["status"] != "running":
        return
    streams = run["stats"].get("streams", {})
    if any(streams.get(s.name, {}).get("status") not in {"done", "failed"}
           for s in source.plan.streams):
        return
    failed = [n for n, s in streams.items() if s.get("status") == "failed"]
    status = "succeeded" if not failed else ("partial" if len(failed) < len(streams)
                                             else "failed")
    error = "; ".join(f"{n}: {streams[n].get('error', 'failed')}" for n in failed) or None
    reg.finish_run(run_id, status, error)
    current = reg.get(source.name)
    if current is not None and current.state not in {"needs_reauth", "paused"}:
        current.state = "active" if status != "failed" else "error"
        reg.save(current)
    log_event(logger, "mcp_run_finished", source=source.name, run_id=run_id, status=status)


def _fail_stream(reg: Registry, source: Source, run_id: str, stream: str, err: Exception
                 ) -> None:
    code = getattr(err, "code", type(err).__name__)
    reg.note_stream(run_id, stream, "failed", f"{code}: {sanitize(str(err), 300)}")
    log_event(logger, "mcp_stream_failed", level=logging.ERROR, source=source.name,
              stream=stream, run_id=run_id, code=code)
    _maybe_finish(reg, source, run_id)


def _authorization_failed(reg: Registry, source: Source, run_id: str | None, err: Exception
                          ) -> None:
    _set_state(reg, source.name, "needs_reauth")
    message = (f"needs_authorization: {sanitize(str(err), 200)}. Run: aquifer sources "
               f"authorize {source.name}")
    if run_id:
        for stream in source.plan.streams:
            run = reg.run(run_id) or {}
            if run.get("stats", {}).get("streams", {}).get(stream.name, {}).get(
                    "status") not in {"done", "failed"}:
                reg.note_stream(run_id, stream.name, "failed", message)
        _maybe_finish(reg, source, run_id)
    log_event(logger, "mcp_needs_reauth", level=logging.WARNING, source=source.name)


# --- handlers (registered with the node service) ----------------------------------------

def handle_sync(service, job) -> None:  # noqa: ANN001
    settings = service.settings
    reg = registry_for(settings)
    source = reg.get(job.payload["source"])
    if source is None or not source.enabled:
        return
    run_id = reg.start_run(source.name)
    try:
        tools = asyncio.run(_list_tools(source, settings))
    except Exception as exc:  # noqa: BLE001
        err = exc if isinstance(exc, IntegratorError) else classify_failure(exc)
        if err.code == "needs_authorization":
            _authorization_failed(reg, source, run_id, err)
            return
        reg.finish_run(run_id, "failed", f"{err.code}: {sanitize(str(err), 300)}")
        if isinstance(err, (SourceUnavailable, SourceRateLimited)):
            raise err from None  # retried by the queue
        _set_state(reg, source.name, "error")
        return
    problems = validate_plan(source, tools, allow_unannotated=source.allow_unannotated)
    if problems:
        reg.finish_run(run_id, "failed", "plan_invalid: " + "; ".join(problems))
        _set_state(reg, source.name, "error")
        log_event(logger, "mcp_plan_invalid", level=logging.ERROR, source=source.name,
                  problems=problems)
        return
    full_requested = bool(job.payload.get("full"))
    every = max(1, settings.integrator.full_listing_every_runs)
    for stream in source.plan.streams:
        st = reg.stream_state(source.name, stream.name)
        full = full_requested or (st.get("runs", 0) % every == 0)
        reg.note_stream(run_id, stream.name, "running")
        service.queue.put("mcp_page", {"source": source.name, "run_id": run_id,
                                       "stream": stream.name, "cursor": None, "page": 0,
                                       "items": 0, "full": full})
    _set_state(reg, source.name, "active")
    log_event(logger, "mcp_run_started", source=source.name, run_id=run_id,
              streams=len(source.plan.streams))


async def _list_tools(source: Source, settings: Settings) -> list:
    async with connect(source, redirect_uri=redirect_uri(settings),
                       allow_private=settings.integrator.allow_private_networks) as conn:
        return await conn.list_tools()


def handle_page(service, job) -> None:  # noqa: ANN001
    from aquifer.node.service import _Postponed

    settings = service.settings
    reg = registry_for(settings)
    p = job.payload
    source = reg.get(p["source"])
    if source is None:
        return
    stream = next((s for s in source.plan.streams if s.name == p["stream"]), None)
    if not source.enabled or stream is None:
        reg.note_stream(p["run_id"], p["stream"], "failed",
                        "source disabled or stream removed during the run")
        if source.enabled:
            _maybe_finish(reg, source, p["run_id"])
        return
    try:
        asyncio.run(_page(settings, reg, source, stream, p, service))
    except SourceRateLimited as exc:
        delay = max(1, int(exc.retry_after))
        service.queue.postpone(job, delay)
        log_event(logger, "mcp_rate_limited", level=logging.WARNING, source=source.name,
                  stream=stream.name, delay_s=delay)
        raise _Postponed from None
    except NeedsAuthorization as exc:
        _authorization_failed(reg, source, p["run_id"], exc)
    except IntegratorError as exc:
        if exc.code == "needs_authorization":
            _authorization_failed(reg, source, p["run_id"], exc)
        elif isinstance(exc, SourceUnavailable):
            if job.attempts >= service.queue.max_attempts:
                _fail_stream(reg, source, p["run_id"], stream.name, exc)
            raise
        else:
            _fail_stream(reg, source, p["run_id"], stream.name, exc)
    except ValueError as exc:  # unmappable results, bad plan paths
        _fail_stream(reg, source, p["run_id"], stream.name, exc)
    except Exception as exc:
        if job.attempts >= service.queue.max_attempts:
            _fail_stream(reg, source, p["run_id"], stream.name, exc)
        raise


async def _page(settings: Settings, reg: Registry, source: Source, stream, p: dict,  # noqa: ANN001
                service) -> None:  # noqa: ANN001
    st = reg.stream_state(source.name, stream.name)
    high_water = None if p.get("full") else _dt(st.get("high_water"))
    pipeline = pipeline_for(settings)
    await asyncio.sleep(settings.integrator.min_call_interval_s)
    async with connect(source, redirect_uri=redirect_uri(settings),
                       allow_private=settings.integrator.allow_private_networks) as conn:
        res = await run_page(conn, source, stream, p.get("cursor"), high_water=high_water,
                             overlap_s=settings.integrator.overlap_s, pipeline=pipeline,
                             remaining_items=max(0, stream.max_items - p.get("items", 0)))
        reg.mark_seen(p["run_id"], stream.name, res.seen)
        reg.add_stats(p["run_id"], pages=1, fetched=res.fetched, indexed=res.indexed,
                      unchanged=res.unchanged, unmappable=res.failed,
                      detail_calls=res.detail_calls)
        run_max = st.get("run_max", {})
        if res.max_updated and res.max_updated > run_max.get(p["run_id"], ""):
            st["run_max"] = {p["run_id"]: res.max_updated}  # only the current run is kept
            reg.set_stream_state(source.name, stream.name, st)
        last_page = p["page"] + 1 >= stream.max_pages
        if not res.done and not last_page:
            service.queue.put("mcp_page", {**p, "cursor": res.next_cursor, "page": p["page"] + 1,
                                           "items": p.get("items", 0) + len(res.seen)})
            return
        # The stream is finished for this run.
        complete = (stream.enumeration == "complete" and res.done
                    and not res.stopped_at_high_water and not res.truncated)
        report = {}
        if complete:
            report = await reconcile_stream(conn, source, stream,
                                            reg.seen(p["run_id"], stream.name),
                                            pipeline.vector_store)
            reg.add_stats(p["run_id"], deleted=report.get("deleted", 0))
    st = reg.stream_state(source.name, stream.name)
    new_max = (st.get("run_max") or {}).get(p["run_id"])
    if new_max and new_max > (st.get("high_water") or ""):
        st["high_water"] = new_max
    st["runs"] = st.get("runs", 0) + 1
    st["last_finished"] = datetime.now(UTC).isoformat()
    if complete:
        st["last_complete_listing"] = st["last_finished"]
    st.pop("run_max", None)
    if report.get("suspended"):
        st["deletion_suspended"] = report["suspended"]
    else:
        st.pop("deletion_suspended", None)
    reg.set_stream_state(source.name, stream.name, st)
    status_note = f"deletions suspended: {report['suspended']}" if report.get("suspended") \
        else None
    reg.note_stream(p["run_id"], stream.name, "done", status_note)
    _maybe_finish(reg, source, p["run_id"])


def _dt(value) -> datetime | None:  # noqa: ANN001
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def handle_authorize(service, job) -> None:  # noqa: ANN001
    settings = service.settings
    reg = registry_for(settings)
    request_id = job.payload["request_id"]
    source = reg.get(job.payload["source"])
    if source is None:
        reg.oauth_update(request_id, status="failed", error="unknown source")
        return

    async def on_redirect(url: str) -> None:
        state = (parse_qs(urlparse(url).query).get("state") or [""])[0]
        reg.oauth_update(request_id, auth_url=url, status="waiting", oauth_state=state)

    async def on_callback() -> tuple[str, str | None]:
        deadline = time.monotonic() + settings.integrator.authorize_timeout_s
        while time.monotonic() < deadline:
            row = reg.oauth_get(request_id) or {}
            if row.get("status") == "code_received":
                return row["code"], row.get("oauth_state")
            if row.get("status") == "failed":
                raise NeedsAuthorization(row.get("error") or "authorization was denied")
            await asyncio.sleep(1)
        raise NeedsAuthorization("no browser callback in time; run authorize again")

    async def check() -> int:
        async with connect(source, redirect_uri=redirect_uri(settings),
                           allow_private=settings.integrator.allow_private_networks,
                           redirect_handler=on_redirect, callback_handler=on_callback) as conn:
            return len(await conn.list_tools())

    try:
        n = asyncio.run(check())
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        err = exc if isinstance(exc, IntegratorError) else classify_failure(exc)
        reg.oauth_update(request_id, status="failed",
                         error=f"{err.code}: {sanitize(str(err), 300)}")
        return
    reg.oauth_update(request_id, status="succeeded", error=None)
    current = reg.get(source.name)
    if current is not None:
        current.state = "active" if current.enabled else "ready"
        reg.save(current)
    log_event(logger, "mcp_authorized", source=source.name, tools=n)


def schedule_sources(service, now: float) -> list[str]:  # noqa: ANN001
    """Queue a run for every enabled source whose schedule is due."""
    reg = registry_for(service.settings)
    added = []
    for source in reg.all():
        if not source.enabled or source.state in {"needs_reauth", "paused",
                                                  "needs_authorization"}:
            continue
        key = f"scheduler:mcp:{source.name}"
        last = service.queue.get_state(key, 0)
        if now - last < source.schedule_hours * 3600:
            continue
        if service.queue.put("mcp_sync", {"source": source.name},
                             dedupe_key=f"mcp_sync:{source.name}"):
            added.append(f"mcp_sync:{source.name}")
        service.queue.set_state(key, now)
    return added


_registered = False


def register() -> None:
    global _registered
    if _registered:
        return
    _registered = True
    from aquifer.node.service import register_handler, register_scheduler

    register_handler("mcp_sync", handle_sync)
    register_handler("mcp_page", handle_page)
    register_handler("mcp_authorize", handle_authorize)
    register_scheduler(schedule_sources)
