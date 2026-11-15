"""The MCP server (streamable HTTP at /mcp) and its routing inside the main app.

Tools are thin wrappers over the service layer (spec: "One service layer").
Phase 4 starts with one read-only tool to prove the connector's OAuth flow end
to end; the rest of the tool surface is built on top once that works.
"""

import sqlite3
from collections.abc import Callable
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from tether.config import Settings
from tether.mcp_server.provider import ConsentRequests, TetherOAuthProvider
from tether.services import sync_state

MCP_PATH = "/mcp"
# Paths served by the MCP SDK app; everything else goes to the main FastAPI app.
_EXACT = {MCP_PATH, "/authorize", "/token", "/register", "/revoke"}
_PREFIXES = ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource")


def is_mcp_path(path: str) -> bool:
    return path in _EXACT or path.startswith(_PREFIXES)


def build(settings: Settings, get_conn: Callable[[], sqlite3.Connection]) -> tuple[MCPServer, TetherOAuthProvider, ASGIApp]:
    base = settings.base_url.rstrip("/")
    resource = base + MCP_PATH
    provider = TetherOAuthProvider(
        get_conn, base_url=base, resource=resource, consent=ConsentRequests(settings.session_secret),
        redirect_hosts={h.strip().lower() for h in settings.mcp_redirect_hosts.split(",") if h.strip()},
    )
    server = MCPServer(
        name="tether",
        title="Tether",
        instructions="Tether is the owner's inbox, task list and calendar hub across all their accounts.",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base,
            resource_server_url=resource,
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    @server.tool(description="Health of each connected source: last successful sync, failures and errors.")
    async def sync_status() -> list[dict]:
        return sync_state.sync_status(get_conn())

    host = urlparse(base).netloc
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Stateless JSON: no sessions to lose on restart, nothing to stream yet.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[base, "https://claude.ai", "https://claude.com"],
        ),
    )
    return server, provider, app


class MCPRouter:
    """ASGI middleware that sends the MCP and OAuth paths to the MCP SDK app."""

    def __init__(self, app: ASGIApp, mcp_app: ASGIApp):
        self.app = app
        self.mcp_app = mcp_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_mcp_path(scope["path"]):
            await self.mcp_app(scope, receive, send)
        else:
            await self.app(scope, receive, send)
