"""FastAPI app: login, account connection, REST API, and the PWA's static files.

Start it with `python -m tether serve`, which always runs exactly one worker
(spec: one-worker rule). Host and port come from TETHER_HOST and TETHER_PORT.
"""

import asyncio
import contextlib
import html
import logging
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi import Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from mcp.server.auth.provider import construct_redirect_uri
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from tether import db
from tether.config import Settings, get_settings
from tether.connectors import google_oauth
from tether.connectors.gmail import GmailClients
from tether.connectors.google_oauth import AuthError
from tether.crypto import Vault
from tether.mcp_server import server as mcp
from tether.mcp_server.provider import CONSENT_PATH
from tether.pollers import CalendarPoller, GmailPoller
from tether.services import accounts, calendar, digest, items, mail, overview, sync_state, tasks, triage

log = logging.getLogger(__name__)


class SyncSettingsUpdate(BaseModel):
    excluded_categories: list[str] | None = None
    initial_sync_days: int | None = None


class AccountUpdate(BaseModel):
    color: str


class TaskCreate(BaseModel):
    title: str
    parent_id: int | None = None
    item_id: int | None = None
    notes: str | None = None
    due_at: str | None = None
    priority: str | None = None


class DigestIn(BaseModel):
    raw_text: str
    digest_date: str | None = None


class SourceUpdate(BaseModel):
    enabled: bool


class HoldRelease(BaseModel):
    action: str


class EventCreate(BaseModel):
    source_id: int
    start: str
    end: str
    time_zone: str
    title: str = ""
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    guests: list[str] = []
    meet: bool = False
    busy: str = "busy"
    repeat: str | None = None
    notify: bool = True


class RestoreTarget(BaseModel):
    source_id: int
    event_id: str = Field(min_length=1, max_length=1024)


class EventRestore(BaseModel):
    undo: list[RestoreTarget] = Field(min_length=1, max_length=50)


class TaskUpdate(BaseModel):
    """Only the fields sent are changed; an explicit null clears one."""
    title: str | None = None
    notes: str | None = None
    due_at: str | None = None
    priority: str | None = None
    sort_order: float | None = None
    parent_id: int | None = None


USER_ERRORS = (triage.TriageError, tasks.TaskError, mail.MailError, digest.DigestError, calendar.CalendarError)


@contextlib.contextmanager
def user_errors():
    """Map service errors (whose messages are safe to show) to HTTP errors."""
    try:
        yield
    except USER_ERRORS as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _calendar_changed(app: FastAPI) -> None:
    poller = getattr(app.state, "calendar", None)
    if poller:
        poller.mark_dirty()
        poller.wake()


def create_app(settings: Settings, *, conn: sqlite3.Connection | None = None, start_pollers: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conn = conn or db.connect(settings.db_path)
        app.state.vault = Vault(settings.encryption_key)
        app.state.http = httpx.AsyncClient(timeout=30)
        app.state.gmail = GmailPoller(app.state.conn, app.state.vault, app.state.http, settings)
        app.state.gmail_clients = GmailClients(app.state.http, app.state.conn, app.state.vault,
                                               settings.google_client_id, settings.google_client_secret)
        app.state.calendar = CalendarPoller(app.state.conn, app.state.vault, app.state.http, settings)
        tasks = [asyncio.create_task(app.state.gmail.run_forever()),
                 asyncio.create_task(app.state.calendar.run_forever())] if start_pollers else []
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            for task in tasks:
                task.cancel()
            await app.state.http.aclose()

    app = FastAPI(title="Tether", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=settings.secure_cookies,
                       same_site="lax", max_age=60 * 60 * 24 * 30)
    # MCP endpoint and its OAuth server (metadata, /register, /authorize, /token, /revoke).
    mcp_server, mcp_provider, mcp_app = mcp.build(
        settings, lambda: app.state.conn, lambda account_id: app.state.gmail_clients(account_id),
        on_calendar_change=lambda: _calendar_changed(app))
    app.add_middleware(mcp.MCPRouter, mcp_app=mcp_app)

    # Dependencies are async so FastAPI runs them on the loop thread, not a threadpool.
    async def conn_dep(request: Request) -> sqlite3.Connection:
        return request.app.state.conn

    async def require_user(request: Request) -> str:
        user = request.session.get("user")
        if not user:
            raise HTTPException(401, "sign in required")
        return user

    # ---------------------------------------------------------------- auth

    def _start_oauth(request: Request, purpose: str) -> RedirectResponse:
        state = secrets.token_urlsafe(24)
        request.session["oauth"] = {"state": state, "purpose": purpose}
        url = google_oauth.authorization_url(
            client_id=settings.google_client_id, redirect_uri=settings.redirect_uri, state=state,
            scopes=google_oauth.LOGIN_SCOPES if purpose == "login" else google_oauth.MAILBOX_SCOPES,
            offline=purpose == "connect",
            login_hint=settings.owner_email if purpose == "login" else None,
        )
        return RedirectResponse(url, status_code=303)

    @app.get("/auth/login")
    async def login(request: Request):
        return _start_oauth(request, "login")

    @app.post("/auth/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/accounts/google/connect")
    async def connect_google(request: Request):
        if not request.session.get("user"):
            return RedirectResponse("/auth/login", status_code=303)
        return _start_oauth(request, "connect")

    @app.get("/auth/google/callback")
    async def google_callback(request: Request, code: str | None = None, state: str | None = None,
                              error: str | None = None):
        pending = request.session.pop("oauth", None)
        if error:
            raise HTTPException(400, f"Google returned: {error}")
        if not pending or not state or not secrets.compare_digest(pending["state"], state) or not code:
            raise HTTPException(400, "invalid OAuth state; start again")
        try:
            token = await google_oauth.exchange_code(
                request.app.state.http, client_id=settings.google_client_id,
                client_secret=settings.google_client_secret, redirect_uri=settings.redirect_uri, code=code)
            identity = google_oauth.identity_from_id_token(token["id_token"], client_id=settings.google_client_id)
        except (AuthError, KeyError) as exc:
            raise HTTPException(400, f"sign-in failed: {exc}") from exc

        if pending["purpose"] == "login":
            if not (identity.email_verified and identity.email == settings.owner_email.lower()):
                raise HTTPException(403, "this Google account is not allowed")
            if settings.owner_sub and identity.sub != settings.owner_sub:
                raise HTTPException(403, "this Google account is not allowed")
            if not settings.owner_sub:
                log.warning("TETHER_OWNER_SUB is unset; pin login by setting it to %s", identity.sub)
            request.session["user"] = identity.email
            dest = request.session.pop("after_login", "/")
            if not dest.startswith("/") or dest.startswith("//"):
                dest = "/"
            return RedirectResponse(dest, status_code=303)

        # purpose == "connect": a mailbox. Requires a signed-in owner.
        if not request.session.get("user"):
            raise HTTPException(401, "sign in required")
        granted = token.get("scope", "")
        if google_oauth.GMAIL_MODIFY not in granted.split():
            raise HTTPException(400, "Gmail access was not granted; tick the Gmail box on the consent screen")
        if "refresh_token" not in token:
            raise HTTPException(400, "Google returned no refresh token; remove Tether's access and reconnect")
        accounts.upsert_gmail_account(
            request.app.state.conn, request.app.state.vault, email=identity.email,
            secret=google_oauth.secret_from_token_response(token, identity), scopes=granted)
        request.app.state.gmail.wake()
        return RedirectResponse("/settings", status_code=303)

    # ---------------------------------------------------- MCP consent page

    @app.get(CONSENT_PATH)
    async def mcp_consent(request: Request, req: str):
        pending = mcp_provider.consent.load(req)
        if pending is None:
            raise HTTPException(400, "this authorization request expired; start the connection again")
        if not request.session.get("user"):
            request.session["after_login"] = f"{CONSENT_PATH}?{request.url.query}"
            return RedirectResponse("/auth/login", status_code=303)
        csrf = secrets.token_urlsafe(24)
        request.session["mcp_csrf"] = csrf
        return HTMLResponse(_consent_page(pending, req, csrf, request.session["user"]))

    @app.post(CONSENT_PATH)
    async def mcp_consent_decision(request: Request, req: str = Form(...), csrf: str = Form(...),
                                   decision: str = Form(...)):
        user = request.session.get("user")
        expected = request.session.pop("mcp_csrf", None)
        if not user or not expected or not secrets.compare_digest(expected, csrf):
            raise HTTPException(403, "consent check failed; start the connection again")
        pending = mcp_provider.consent.load(req)
        if pending is None:
            raise HTTPException(400, "this authorization request expired; start the connection again")
        if decision != "approve":
            target = construct_redirect_uri(pending["redirect_uri"], error="access_denied", state=pending["state"])
        else:
            code = mcp_provider.approve(pending, subject=user)
            target = construct_redirect_uri(pending["redirect_uri"], code=code, state=pending["state"])
        return RedirectResponse(target, status_code=303)

    # ----------------------------------------------------------------- api

    @app.get("/api/me")
    async def me(user: str = Depends(require_user)):
        return {"email": user}

    @app.get("/api/items")
    async def list_items(account: str | None = None, state: str | None = "open", limit: int = 50, offset: int = 0,
                         _: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        if state not in (None, "open", "archived"):
            raise HTTPException(422, "state must be open or archived")
        return {"items": items.list_items(conn, account=account, state=state, limit=limit, offset=offset),
                "total": items.count_items(conn, account=account, state=state)}

    @app.get("/api/items/{item_id}/thread")
    async def item_thread(item_id: int, request: Request, _: str = Depends(require_user)):
        with user_errors():
            return await mail.get_item_thread(request.app.state.conn, request.app.state.gmail_clients, item_id)

    async def _triage(request: Request, action, item_id: int) -> dict:
        with user_errors():
            return await action(request.app.state.conn, request.app.state.gmail_clients, item_id)

    @app.post("/api/items/{item_id}/archive")
    async def archive_item(item_id: int, request: Request, _: str = Depends(require_user)):
        return await _triage(request, triage.archive_item, item_id)

    @app.post("/api/items/{item_id}/unarchive")
    async def unarchive_item(item_id: int, request: Request, _: str = Depends(require_user)):
        return await _triage(request, triage.unarchive_item, item_id)

    def account_view(a: sqlite3.Row) -> dict:
        view = {"id": a["id"], "kind": a["kind"], "label": a["label"], "color": a["color"], "status": a["status"]}
        if a["kind"] == "gmail":
            view["sync"] = {
                "excluded_categories": sorted(accounts.excluded_categories(a)),
                "initial_sync_days": accounts.sync_days(a, settings.initial_sync_days),
                "categories": list(accounts.GMAIL_CATEGORIES),
                "max_days": accounts.MAX_SYNC_DAYS,
            }
        return view

    @app.get("/api/accounts")
    async def list_accounts(_: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        return [account_view(a) for a in accounts.list_accounts(conn)]

    @app.patch("/api/accounts/{account_id}")
    async def update_account(account_id: int, body: AccountUpdate,
                             _: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        try:
            accounts.set_color(conn, account_id, body.color)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return account_view(accounts.get_account(conn, account_id))

    @app.patch("/api/accounts/{account_id}/sync")
    async def update_sync(account_id: int, body: SyncSettingsUpdate, request: Request,
                          _: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        try:
            changed = accounts.update_sync_settings(
                conn, account_id, excluded_categories=body.excluded_categories,
                initial_sync_days=body.initial_sync_days)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if changed:
            request.app.state.gmail.wake()
        return {"changed": changed, "account": account_view(accounts.get_account(conn, account_id))}

    @app.get("/api/sync-status")
    async def get_sync_status(_: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        return sync_state.sync_status(conn)

    @app.get("/api/overview")
    async def get_overview(_: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        return overview.get_overview(conn, settings.today(), settings.tz)

    # --------------------------------------------------------------- tasks

    @app.get("/api/tasks")
    async def list_tasks(status: str | None = "open", _: str = Depends(require_user),
                         conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            return tasks.list_tasks(conn, status=status)

    @app.get("/api/tasks/page")
    async def task_page(offset: int = 0, limit: int = 50, _: str = Depends(require_user),
                        conn: sqlite3.Connection = Depends(conn_dep)):
        return tasks.task_page(conn, offset=offset, limit=limit)

    @app.post("/api/tasks", status_code=201)
    async def create_task(body: TaskCreate, _: str = Depends(require_user),
                          conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            return tasks.create_task(conn, **body.model_dump())

    @app.patch("/api/tasks/{task_id}")
    async def update_task(task_id: int, body: TaskUpdate, _: str = Depends(require_user),
                          conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            return tasks.update_task(conn, task_id, body.model_dump(exclude_unset=True))

    @app.post("/api/tasks/{task_id}/complete")
    async def complete_task(task_id: int, request: Request, _: str = Depends(require_user)):
        with user_errors():
            return await tasks.complete_task(request.app.state.conn, request.app.state.gmail_clients, task_id)

    @app.post("/api/tasks/{task_id}/reopen")
    async def reopen_task(task_id: int, _: str = Depends(require_user),
                          conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            return tasks.reopen_task(conn, task_id)

    @app.delete("/api/tasks/{task_id}", status_code=204)
    async def delete_task(task_id: int, _: str = Depends(require_user),
                          conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            tasks.delete_task(conn, task_id)

    # ---------------------------------------------------------------- mail

    @app.get("/api/mail/search")
    async def search_mail(q: str, request: Request, account: str | None = None, limit: int = mail.DEFAULT_LIMIT,
                          _: str = Depends(require_user)):
        with user_errors():
            return await mail.search_mail(request.app.state.conn, request.app.state.gmail_clients, q,
                                          account=account, limit=limit)

    # ------------------------------------------------------------ calendar

    def hub(conn: sqlite3.Connection) -> calendar.Hub | None:
        return calendar.hub_config(conn, settings.hub_account, settings.hub_calendar_id)

    @app.get("/api/agenda")
    async def get_agenda(start: str | None = None, end: str | None = None, _: str = Depends(require_user),
                         conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            first, last = calendar.parse_range(start, end, settings.today())
            return calendar.get_agenda(conn, first, last, settings.tz, actions=True, hub=hub(conn))

    @app.get("/api/calendar")
    async def get_calendar(_: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        return calendar.calendar_status(conn, hub(conn), settings.hub_account)

    @app.patch("/api/calendar/sources/{source_id}")
    async def update_source(source_id: int, body: SourceUpdate, request: Request, _: str = Depends(require_user),
                            conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            result = calendar.set_source_enabled(conn, source_id, body.enabled, hub(conn))
        request.app.state.calendar.mark_dirty()
        request.app.state.calendar.wake()
        return result

    @app.post("/api/calendar/sources/{source_id}/release")
    async def release_hold(source_id: int, body: HoldRelease, request: Request, _: str = Depends(require_user),
                           conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            result = calendar.release_hold(conn, source_id, body.action)
        request.app.state.calendar.mark_dirty()
        request.app.state.calendar.wake()
        return result

    @app.post("/api/calendar/events", status_code=201)
    async def create_event(body: EventCreate, request: Request, _: str = Depends(require_user),
                           conn: sqlite3.Connection = Depends(conn_dep)):
        poller = request.app.state.calendar
        with user_errors():
            source = calendar.get_source(conn, body.source_id)
            result = await calendar.create_event(conn, poller.client(source["account_id"]),
                                                 calendar.NewEvent(**body.model_dump()), hub(conn))
        # Fetch that calendar now so the new event is on the agenda when this returns.
        try:
            await asyncio.wait_for(poller.refresh_source(body.source_id), timeout=15)
        except Exception as exc:  # the event exists; the next cycle picks it up
            log.info("calendar source %s: refresh after create failed: %s", body.source_id, type(exc).__name__)
        return result

    @app.delete("/api/calendar/events/{event_id}")
    async def delete_event(event_id: int, request: Request, series: bool = False, notify: bool = False,
                           _: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        """event_id is the agenda event's id. Held under the poller's lock, so a fetch that started
        before the delete can't put the event back."""
        poller = request.app.state.calendar
        async with poller.lock:
            with user_errors():
                result = await calendar.delete_event(conn, poller.client, event_id, series=series, notify=notify,
                                                     hub=hub(conn))
        poller.wake()   # write the hub: its copy goes too
        return result

    @app.post("/api/calendar/events/restore")
    async def restore_events(body: EventRestore, request: Request, _: str = Depends(require_user),
                             conn: sqlite3.Connection = Depends(conn_dep)):
        poller = request.app.state.calendar
        with user_errors():
            sources = await calendar.restore_events(conn, poller.client, [u.model_dump() for u in body.undo],
                                                    hub(conn))
        for source_id in sources:
            try:
                await asyncio.wait_for(poller.refresh_source(source_id), timeout=15)
            except Exception as exc:  # restored in Google; the next cycle picks it up
                log.info("calendar source %s: refresh after restore failed: %s", source_id, type(exc).__name__)
        return {"restored": len(body.undo)}

    @app.post("/api/calendar/sync")
    async def calendar_sync(request: Request, _: str = Depends(require_user)):
        request.app.state.calendar.wake(refetch=True)
        return {"ok": True}

    @app.post("/api/digest")
    async def ingest_digest(body: DigestIn, _: str = Depends(require_user),
                            conn: sqlite3.Connection = Depends(conn_dep)):
        with user_errors():
            return digest.ingest_digest(conn, body.raw_text, tz=settings.tz, today=settings.today(),
                                        digest_date=body.digest_date, label=settings.digest_label)

    # -------------------------------------------------------------- static

    static_dir = Path(settings.static_dir)
    if (static_dir / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets", check_dir=False), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str, request: Request):
            if path.startswith(("api/", "auth/", "accounts/", ".well-known/")):
                raise HTTPException(404)
            # The home page Google's consent screen links to: public, says what Tether is and links the policies.
            # Signed in, / is the app as usual.
            if path in ("", "about") and (path == "about" or not request.session.get("user")) \
                    and (static_dir / "home.html").is_file():
                return FileResponse(static_dir / "home.html", headers={"Cache-Control": "no-cache", "X-Tether-Page": "home"})
            candidate = (static_dir / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


def _consent_page(pending: dict, req: str, csrf: str, user: str) -> str:
    client = html.escape(pending["client_name"] or "An MCP client")
    dest = html.escape(urlparse(pending["redirect_uri"]).netloc)
    e = html.escape
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tether · Connect {client}</title>
<style>
  :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; line-height: 1.5; }}
  body {{ max-width: 460px; margin: 0 auto; padding: 48px 16px; }}
  h1 {{ font-size: 1.3rem; }} .muted {{ opacity: .7; font-size: .9rem; }}
  form {{ display: flex; gap: 12px; margin-top: 24px; }}
  button {{ font: inherit; padding: 10px 18px; border-radius: 8px; border: 1px solid #8886; cursor: pointer; }}
  button[value=approve] {{ background: #1f2a44; color: #fff; border-color: #1f2a44; }}
</style></head><body>
<h1>Allow {client} to use Tether?</h1>
<p>It will be able to read your inbox items, mail, tasks and calendar in Tether, and change tasks and
items on your behalf.</p>
<p class="muted">Signed in as {e(user)}. After you choose, you'll return to <strong>{dest}</strong>.</p>
<form method="post" action="{CONSENT_PATH}">
  <input type="hidden" name="req" value="{e(req)}"><input type="hidden" name="csrf" value="{e(csrf)}">
  <button name="decision" value="approve">Allow</button>
  <button name="decision" value="deny">Deny</button>
</form>
</body></html>"""


class RedactQueryStrings(logging.Filter):
    """Strip query strings from access-log lines. The OAuth callback's query
    carries Google's authorization code, and logs never contain credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0] + "?…", *args[3:])
        return True


def main_app() -> FastAPI:
    """App factory used by `python -m tether serve`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(RedactQueryStrings())
    # httpx logs every request URL at INFO; Gmail URLs carry thread IDs and queries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return create_app(get_settings())
