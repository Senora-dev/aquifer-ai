"""Connection health, progress and actionable errors for `aquifer sources status`."""

from __future__ import annotations

from aquifer.integrator.jobs import registry_for

ACCESS_NOTE = ("Everything this source imports is readable by every Aquifer MCP token of this "
               "deployment (one trust domain), regardless of permissions in the original tool. "
               "What is imported is limited by the access of the account that authorized it.")

_HINTS = {
    "needs_authorization": "Authorize it: aquifer sources authorize {name}",
    "needs_reauth": "The stored credentials stopped working (expired, revoked or rotated). "
                    "Re-authorize: aquifer sources authorize {name}",
    "paused": "Paused. Resume with: aquifer sources enable {name}",
    "error": "The last run failed; see last_run.error. After fixing it: aquifer sources sync "
             "{name}",
}


def _hint_for_error(error: str | None, name: str) -> str | None:
    if not error:
        return None
    if "plan_invalid" in error:
        return (f"The server's tools changed. Inspect: aquifer sources tools {name}; then fix "
                f"the plan (aquifer sources plan {name}).")
    if "rate_limited" in error:
        return "The source rate-limited requests; pages are retried automatically later."
    if "unsafe_destination" in error:
        return ("The URL (or an OAuth endpoint it advertised) is not a public HTTPS address. "
                "Only set AQUIFER_INTEGRATOR__ALLOW_PRIVATE_NETWORKS=true for trusted internal "
                "servers.")
    if "tool_not_approved" in error:
        return f"Approve the tool by updating the plan: aquifer sources plan {name}"
    if "source_unavailable" in error:
        return "The server could not be reached; retried with backoff on the next run."
    return None


def sources_status(settings, queue=None) -> list[dict]:  # noqa: ANN001
    reg = registry_for(settings)
    out = []
    for s in reg.all():
        runs = reg.runs(s.name, limit=1)
        last = runs[0] if runs else None
        entry = {
            "name": s.name, "provider": s.provider, "url": s.url, "auth": s.auth.type,
            "state": s.state, "enabled": s.enabled, "schedule_hours": s.schedule_hours,
            "scope": s.scope,
            "last_success": reg.last_success(s.name),
            "last_run": last,
            "streams": [{**st.coverage(), **{k: v for k, v in reg.stream_state(
                s.name, st.name).items() if k in {"high_water", "last_finished",
                                                  "last_complete_listing",
                                                  "deletion_suspended"}}}
                        for st in s.plan.streams],
            "access": ACCESS_NOTE,
        }
        hint = _HINTS.get(s.state, "").format(name=s.name) or _hint_for_error(
            (last or {}).get("error"), s.name)
        if hint:
            entry["action"] = hint
        if last and last["status"] == "running":
            entry["progress"] = last["stats"]
        if queue is not None:
            dead = [d for d in queue.dead_letters(limit=50)
                    if d["kind"].startswith("mcp_") and s.name in d["summary"]]
            if dead:
                entry["dead_letters"] = dead
        out.append(entry)
    return out
