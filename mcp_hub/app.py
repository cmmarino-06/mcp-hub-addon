"""
mcp-hub: aggregates HA MCP + unifi-mcp behind a single MCP Streamable HTTP
endpoint, so claude.ai only needs ONE custom connector slot.

- Exposes itself at HUB_SECRET_PATH (e.g. /private_<random>) — authless to
  Claude, same pattern as the existing HA MCP connector. The unguessable
  path IS the auth.
- Proxies tools/list and tools/call to each backend, namespacing tool names
  as "<backend>__<original_name>" so ha__* and unifi__* never collide.
- Injects the unifi-mcp bearer token on the backend leg only — Claude never
  sees it, and unifi-mcp's own auth model doesn't need to change.
- Talks to each backend fresh per request (stateless on the backend side).
  This is deliberately simple: personal-scale traffic, no persistent
  backend sessions to leak or go stale.

Verified against `mcp` SDK 1.28.1 APIs directly (not from memory) before
writing this — see chat for the inspection steps.
"""

import contextlib
import logging
import os

import uvicorn
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("mcp-hub")

SEP = "__"  # namespace separator: ha__get_state, unifi__list_clients

# --- Backend registry, built from env / Supervisor options ---------------

def _unifi_headers() -> dict[str, str]:
    token = os.environ.get("UNIFI_MCP_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


BACKENDS: dict[str, dict] = {
    "ha": {
        "url": os.environ["HA_MCP_URL"],
        "headers": {},
    },
    "unifi": {
        "url": os.environ["UNIFI_MCP_URL"],
        "headers": _unifi_headers(),
    },
}

HUB_SECRET_PATH = os.environ["HUB_SECRET_PATH"]  # e.g. "/private_abc123..."
if not HUB_SECRET_PATH.startswith("/"):
    HUB_SECRET_PATH = "/" + HUB_SECRET_PATH


@contextlib.asynccontextmanager
async def backend_session(backend_name: str):
    cfg = BACKENDS[backend_name]
    async with streamablehttp_client(cfg["url"], headers=cfg["headers"]) as (
        read,
        write,
        _get_session_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# --- Aggregator MCP server -------------------------------------------------

server = Server("mcp-hub")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    merged: list[types.Tool] = []
    for backend_name in BACKENDS:
        try:
            async with backend_session(backend_name) as session:
                result = await session.list_tools()
        except Exception:
            log.exception("list_tools failed for backend %r — skipping it this round", backend_name)
            continue
        for tool in result.tools:
            merged.append(
                types.Tool(
                    name=f"{backend_name}{SEP}{tool.name}",
                    description=f"[{backend_name}] {tool.description or ''}",
                    inputSchema=tool.inputSchema,
                )
            )
    return merged


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
    if SEP not in name:
        raise ValueError(f"Unknown tool: {name!r} (missing backend namespace)")
    backend_name, real_name = name.split(SEP, 1)
    if backend_name not in BACKENDS:
        raise ValueError(f"Unknown backend: {backend_name!r}")

    async with backend_session(backend_name) as session:
        result = await session.call_tool(real_name, arguments)
        return list(result.content)


# --- Transport: Streamable HTTP mounted at the secret path -----------------

session_manager = StreamableHTTPSessionManager(
    app=server,
    json_response=True,  # simpler behind Cloudflare Tunnel than raw SSE
    stateless=True,       # no session state to lose on a proxy restart
)


# --- Top-level ASGI app: explicit dispatch, no framework routing ----------
#
# Deliberately NOT using Starlette's Router/Mount/Route here. Two issues
# surfaced going that route:
#   1. Mount()'s default trailing-slash redirect is a 307 built as http://
#      (uvicorn behind Cloudflare Tunnel doesn't know TLS was terminated
#      upstream) — a real downgrade, not just an inconvenience.
#   2. Disabling that redirect makes Mount's regex require the trailing
#      slash to match at all — the bare secret-path URL (what we actually
#      hand out) then 404s. Route() isn't a clean fix either: it detects
#      session_manager.handle_request as a plain bound method and wraps it
#      as func(request) -> response, which is the wrong calling convention
#      for a raw ASGI callable.
# An explicit dispatcher sidesteps all of it: exact string comparison,
# no regex, no trailing-slash convention to fight.

_SECRET_PATH_BARE = HUB_SECRET_PATH.rstrip("/")


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await _session_manager_ctx.__aenter__()
                log.info("mcp-hub ready — backends: %s", list(BACKENDS))
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await _session_manager_ctx.__aexit__(None, None, None)
                await send({"type": "lifespan.shutdown.complete"})
                return
        return

    if scope["type"] != "http":
        return

    path = scope["path"].rstrip("/") or "/"

    if path == "/healthz":
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})
        return

    if path == _SECRET_PATH_BARE:
        await session_manager.handle_request(scope, receive, send)
        return

    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"Not Found"})


_session_manager_ctx = session_manager.run()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9585"))
    uvicorn.run(app, host="0.0.0.0", port=port)
