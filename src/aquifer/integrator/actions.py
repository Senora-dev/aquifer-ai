"""Integrator operations shared by the CLI (`aquifer sources …`) and the UI.

Each function raises ``ValueError`` with an operator-readable message, or
:class:`aquifer.integrator.client.IntegratorError` for a classified connection failure.
"""

from __future__ import annotations

import asyncio
import time

from aquifer.core.config import Settings
from aquifer.integrator.models import Source


def _connect(source: Source, settings: Settings, **kw):  # noqa: ANN003, ANN202
    from aquifer.integrator.client import connect
    from aquifer.integrator.jobs import redirect_uri

    return connect(source, redirect_uri=redirect_uri(settings),
                   allow_private=settings.integrator.allow_private_networks, **kw)


def live_tools(source: Source, settings: Settings) -> list:
    async def run():  # noqa: ANN202
        async with _connect(source, settings) as conn:
            return await conn.list_tools()

    return asyncio.run(run())


def approve(source: Source, stream) -> None:  # noqa: ANN001
    for tool in {stream.tool} | {s.tool for s in (stream.detail, stream.verify) if s}:
        if tool not in source.approved_tools:
            source.approved_tools.append(tool)


def add_stream(source: Source, stream, tools) -> list[str]:  # noqa: ANN001
    from aquifer.integrator.plan import validate_plan

    source.plan.streams = [s for s in source.plan.streams if s.name != stream.name] + [stream]
    approve(source, stream)
    return validate_plan(source, tools, allow_unannotated=source.allow_unannotated)


def add_template_streams(settings: Settings, source: Source, template: str,
                         targets: list[str], *, page_size: int | None = None,
                         tools=None) -> list:  # noqa: ANN001
    """Add a provider's preconfigured stream for each target. Saves the source."""
    from aquifer.integrator.jobs import registry_for
    from aquifer.integrator.providers import get_provider

    provider = get_provider(source.provider) if source.provider != "custom" else None
    if not provider or template not in provider.templates:
        known = ", ".join(provider.templates) if provider else "none (custom source)"
        raise ValueError(f"no template {template!r} for this source; templates: {known}. Use "
                         "`aquifer sources propose` for anything else.")
    live = tools if tools is not None else live_tools(source, settings)
    added = []
    for target in targets:
        stream = provider.templates[template](target)
        if page_size:
            stream.pagination.size = max(1, min(100, page_size))
        problems = add_stream(source, stream, live)
        if problems:
            raise ValueError("; ".join(problems))
        added.append(stream)
    registry_for(settings).save(source)
    return added


def preview(settings: Settings, source: Source, limit: int = 3) -> dict:
    """Sample documents and coverage for every stream. Indexes nothing."""
    from aquifer.integrator.client import sanitize
    from aquifer.integrator.plan import find_items, get_path
    from aquifer.integrator.status import ACCESS_NOTE
    from aquifer.integrator.sync import _detail_args, _text, page_args, to_document

    if not source.plan.streams:
        raise ValueError(f"no streams yet: aquifer sources template|propose {source.name} …")

    async def run():  # noqa: ANN202
        out = []
        async with _connect(source, settings) as conn:
            for stream in source.plan.streams:
                entry = {"stream": stream.name, "coverage": stream.coverage(), "samples": []}
                try:
                    res = await conn.call(stream.tool, page_args(stream, None))
                    items = find_items(res.data, stream.items)
                    entry["items_on_first_page"] = len(items)
                    for item in items[:limit]:
                        doc = to_document(source, stream, item)
                        if stream.detail:
                            d = await conn.call(stream.detail.tool, _detail_args(
                                stream, item, doc.metadata["mcp_item_id"]))
                            body = d.text if not stream.detail.body else _text(
                                get_path(d.data, stream.detail.body))
                            doc = doc.model_copy(update={"body": body})
                        entry["samples"].append({
                            "document_id": doc.id, "title": doc.title, "url": doc.url,
                            "updated_at": doc.updated_at, "author": doc.author,
                            "characters": len(doc.body),
                            "excerpt": sanitize(doc.body, 240)})
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = f"{getattr(exc, 'code', type(exc).__name__)}: " \
                                     f"{sanitize(str(exc), 300)}"
                out.append(entry)
        return out

    return {"source": source.name, "nothing_was_indexed": True, "access": ACCESS_NOTE,
            "streams": asyncio.run(run())}


def enable(settings: Settings, source: Source, *, every_hours: int | None = None,
           queue=None) -> None:  # noqa: ANN001
    """Check the plan against the server's tools now, then queue the first import."""
    from aquifer.integrator.jobs import registry_for
    from aquifer.integrator.plan import validate_plan
    from aquifer.node.service import open_queue

    if not source.plan.streams:
        raise ValueError("the source has no streams to sync")
    problems = validate_plan(source, live_tools(source, settings),
                             allow_unannotated=source.allow_unannotated)
    if problems:
        raise ValueError("; ".join(problems))
    source.enabled = True
    source.state = "active"
    if every_hours:
        source.schedule_hours = every_hours
    registry_for(settings).save(source)
    queue = queue or open_queue(settings)
    queue.put("mcp_sync", {"source": source.name, "full": True},
              dedupe_key=f"mcp_sync:{source.name}")
    queue.set_state(f"scheduler:mcp:{source.name}", time.time())
