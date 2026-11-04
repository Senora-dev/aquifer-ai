"""``GET /oauth/callback``: where the browser returns after authorizing an MCP source.

No bearer token is required (it is a browser redirect). The route only accepts an OAuth
``state`` that belongs to a pending authorization started by `aquifer sources authorize`,
created within the authorization timeout, and still waiting; each state works once. The
response is a static page: nothing from the request is echoed back.
"""

from __future__ import annotations

from urllib.parse import parse_qs

_PAGE = ("<!doctype html><meta charset=utf-8><title>Aquifer</title>"
         "<body style='font-family:system-ui;margin:3rem'><h1>{title}</h1><p>{text}</p></body>")


def oauth_callback_app(app, registry_getter, max_age_s: float = 900):  # noqa: ANN001, ANN201
    async def wrapped(scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http" or scope.get("path") != "/oauth/callback":
            await app(scope, receive, send)
            return
        query = parse_qs(scope.get("query_string", b"").decode(errors="replace"))
        state = (query.get("state") or [""])[0][:200]
        code = (query.get("code") or [None])[0]
        error = (query.get("error") or [None])[0]
        delivered = bool(state) and registry_getter().oauth_deliver(
            state, code=code[:4000] if code else None,
            error=f"provider returned error: {error[:100]}" if error else None,
            max_age_s=max_age_s)
        if delivered and code:
            status, title, text = 200, "Authorized", "Aquifer received the authorization. " \
                "You can close this tab; the terminal shows the result."
        elif delivered:
            status, title, text = 400, "Authorization denied", "The provider did not " \
                "grant access. Run the authorize command again to retry."
        else:
            status, title, text = 400, "Unknown or expired request", "Start again with: " \
                "aquifer sources authorize NAME"
        body = _PAGE.format(title=title, text=text).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                (b"cache-control", b"no-store"),
                                (b"referrer-policy", b"no-referrer")]})
        await send({"type": "http.response.body", "body": body})

    return wrapped
