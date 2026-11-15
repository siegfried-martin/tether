"""FastAPI app: login, account connection, REST API, and the PWA's static files.

Start it with `python -m tether serve`, which always runs exactly one worker
(spec: one-worker rule). Host and port come from TETHER_HOST and TETHER_PORT.
"""

import asyncio
import contextlib
import logging
import secrets
import sqlite3
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from tether import db
from tether.config import Settings, get_settings
from tether.connectors import google_oauth
from tether.connectors.google_oauth import AuthError
from tether.crypto import Vault
from tether.pollers import GmailPoller
from tether.services import accounts, items, sync_state

log = logging.getLogger(__name__)


class SyncSettingsUpdate(BaseModel):
    excluded_categories: list[str] | None = None
    initial_sync_days: int | None = None


def create_app(settings: Settings, *, conn: sqlite3.Connection | None = None, start_pollers: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conn = conn or db.connect(settings.db_path)
        app.state.vault = Vault(settings.encryption_key)
        app.state.http = httpx.AsyncClient(timeout=30)
        app.state.gmail = GmailPoller(app.state.conn, app.state.vault, app.state.http, settings)
        tasks = [asyncio.create_task(app.state.gmail.run_forever())] if start_pollers else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await app.state.http.aclose()

    app = FastAPI(title="Tether", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=settings.secure_cookies,
                       same_site="lax", max_age=60 * 60 * 24 * 30)

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
            return RedirectResponse("/", status_code=303)

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

    # ----------------------------------------------------------------- api

    @app.get("/api/me")
    async def me(user: str = Depends(require_user)):
        return {"email": user}

    @app.get("/api/items")
    async def list_items(account: str | None = None, state: str | None = "open", limit: int = 100,
                         _: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        if state not in (None, "open", "archived"):
            raise HTTPException(422, "state must be open or archived")
        return items.list_items(conn, account=account, state=state, limit=limit)

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
    async def overview(_: str = Depends(require_user), conn: sqlite3.Connection = Depends(conn_dep)):
        open_items = items.list_items(conn, state="open", limit=5)
        count = conn.execute("SELECT COUNT(*) FROM item WHERE state = 'open'").fetchone()[0]
        return {"urgent": sync_state.source_alerts(conn), "inbox": {"count": count, "preview": open_items}}

    # -------------------------------------------------------------- static

    static_dir = Path(settings.static_dir)
    if (static_dir / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets", check_dir=False), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith(("api/", "auth/", "accounts/")):
                raise HTTPException(404)
            candidate = (static_dir / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


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
