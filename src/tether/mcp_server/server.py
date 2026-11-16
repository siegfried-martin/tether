"""The MCP server (streamable HTTP at /mcp) and its routing inside the main app.

Tools are thin wrappers over the service layer (spec: "One service layer").
Each tool calls the same service function as its REST endpoint.
"""

import sqlite3
from collections.abc import Callable
from urllib.parse import urlparse

from typing import Any, Literal

from pydantic import BaseModel, Field

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from tether.config import Settings
from tether.mcp_server.provider import ConsentRequests, TetherOAuthProvider
from tether.services import digest, items, mail, overview, sync_state, tasks, triage
from tether.services.triage import GmailFactory

MCP_PATH = "/mcp"
# Paths served by the MCP SDK app; everything else goes to the main FastAPI app.
_EXACT = {MCP_PATH, "/authorize", "/token", "/register", "/revoke"}
_PREFIXES = ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource")


def is_mcp_path(path: str) -> bool:
    return path in _EXACT or path.startswith(_PREFIXES)


class ThreadRef(BaseModel):
    """A Gmail thread from search_mail results."""
    account: str = Field(description="Account label (mailbox address) from the search result")
    thread_id: str


USER_ERRORS = (triage.TriageError, tasks.TaskError, mail.MailError, digest.DigestError)


def build(settings: Settings, get_conn: Callable[[], sqlite3.Connection],
          gmail: GmailFactory) -> tuple[MCPServer, TetherOAuthProvider, ASGIApp]:
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

    @server.tool(description=(
        "List inbox items (one per conversation). Open items come newest activity first; archived items "
        "come most recently archived first. `account` is an account label such as a mailbox address. "
        "Each item has an id for archive_item and unarchive_item, and a url that opens it in Gmail."))
    async def list_items(account: str | None = None, state: Literal["open", "archived"] = "open",
                         limit: int = 50) -> list[dict]:
        return items.list_items(get_conn(), account=account, state=state, limit=limit)

    @server.tool(description=(
        "Archive an inbox item by id. For Gmail this archives the whole thread in Gmail too. "
        "Returns the updated item."))
    async def archive_item(id: int) -> dict:
        return await _triage(triage.archive_item, id)

    @server.tool(description=(
        "Reopen an archived item by id. For Gmail this moves the thread back to the Gmail inbox. "
        "Returns the updated item, with a `note` if it stays archived in Tether."))
    async def unarchive_item(id: int) -> dict:
        return await _triage(triage.unarchive_item, id)

    async def _triage(action, item_id: int) -> dict:
        return await _guard(action(get_conn(), gmail, item_id))

    async def _guard(awaitable):
        try:
            return await awaitable
        except USER_ERRORS as exc:
            # ToolError's message reaches Claude as-is; other exceptions are masked.
            raise ToolError(str(exc)) from exc

    def _sync_guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except USER_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description=(
        "The home screen as data: urgent things (source alerts, tasks due today or overdue, high-priority "
        "items), inbox counts per account, and open task count. Start here for 'what am I forgetting?'. "
        f"Dates are local to {settings.timezone}."))
    async def get_overview() -> dict:
        return overview.get_overview(get_conn(), settings.today())

    @server.tool(description=(
        "List tasks. status 'open' (default) returns each top-level task followed by its subtasks; 'done' "
        "returns recently completed tasks. Each task shows its linked email (item_title, item_url) if any."))
    async def list_tasks(status: Literal["open", "done"] = "open", parent_id: int | None = None) -> list[dict]:
        return _sync_guard(tasks.list_tasks, get_conn(), status=status, parent_id=parent_id)

    @server.tool(description=(
        "Create a task. Link it to mail with item_id (from list_items) or thread (from search_mail); a thread "
        "is put under sync so a reply reopens it, and completing the task archives it. Tasks are two levels "
        "deep: parent_id must be a top-level task. due_at is a date (YYYY-MM-DD); priority is high, normal "
        "or low."))
    async def create_task(title: str, parent_id: int | None = None, item_id: int | None = None,
                          thread: ThreadRef | None = None, due_at: str | None = None,
                          priority: Literal["high", "normal", "low"] | None = None,
                          notes: str | None = None) -> dict:
        if thread is not None:
            if item_id is not None:
                raise ToolError("pass item_id or thread, not both")
            item_id = await _guard(mail.ensure_item(get_conn(), gmail, thread.account, thread.thread_id))
        return _sync_guard(tasks.create_task, get_conn(), title=title, parent_id=parent_id, item_id=item_id,
                           due_at=due_at, priority=priority, notes=notes)

    @server.tool(description=(
        "Edit a task. fields may set title, notes, due_at (YYYY-MM-DD), priority (high/normal/low), "
        "sort_order (number; lower comes first) or parent_id. A null value clears that field."))
    async def update_task(id: int, fields: dict[str, Any]) -> dict:
        return _sync_guard(tasks.update_task, get_conn(), id, fields)

    @server.tool(description=(
        "Complete a task and its open subtasks. A linked email is archived (in Gmail too) once no other "
        "open task links to it. Returns the task, archived item ids and any warnings."))
    async def complete_task(id: int) -> dict:
        return await _guard(tasks.complete_task(get_conn(), gmail, id))

    @server.tool(description=(
        "Search Gmail with Gmail search syntax (e.g. 'is:unread newer_than:3d', 'from:ann has:attachment'). "
        "Without account, searches every connected mailbox and merges results newest first. Searches the "
        "whole mailbox, not just the inbox. Returns thread summaries; read one with get_thread. "
        f"limit is per account (default {mail.DEFAULT_LIMIT}, max {mail.MAX_LIMIT})."))
    async def search_mail(query: str, account: str | None = None, limit: int = mail.DEFAULT_LIMIT) -> dict:
        return await _guard(mail.search_mail(get_conn(), gmail, query, account=account, limit=limit))

    @server.tool(description=(
        "Read a Gmail thread: each message's sender, recipients, date and body as plain text, with "
        "attachments listed by name. Fetched live and not stored. account is the mailbox address."))
    async def get_thread(account: str, thread_id: str) -> dict:
        return await _guard(mail.get_thread(get_conn(), gmail, account, thread_id))

    @server.tool(description=(
        "Ingest a work-mail digest the owner pasted (ITEM blocks with From, Subject, Received, Needed, Due, "
        "Priority, Link). Pass the pasted text through verbatim as raw_text: do not reformat, summarize or "
        "fill in fields; the server parses it. digest_date (YYYY-MM-DD) is the day the digest was written, "
        "used when a block has no Received time; it defaults to today. Items land in the inbox under the "
        "digest account. Show the owner any rejected blocks and warnings rather than guessing the missing "
        f"fields. Times without an offset are read in {settings.timezone}."))
    async def ingest_digest(raw_text: str, digest_date: str | None = None) -> dict:
        return _sync_guard(digest.ingest_digest, get_conn(), raw_text, tz=settings.tz, today=settings.today(),
                           digest_date=digest_date, label=settings.digest_label)

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
