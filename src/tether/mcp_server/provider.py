"""OAuth authorization server for the MCP endpoint, backed by SQLite.

The MCP SDK serves the protocol endpoints (metadata, /register, /authorize,
/token, /revoke) and validates PKCE and redirect URIs. This class supplies
storage and the one Tether-specific step: /authorize hands off to a consent
page that requires the owner's Google sign-in.
"""

import sqlite3
from collections.abc import Callable
from urllib.parse import urlencode, urlparse

from itsdangerous import BadSignature, URLSafeTimedSerializer
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from tether.services import mcp_auth

CONSENT_PATH = "/auth/mcp/consent"
CONSENT_MAX_AGE = 10 * 60


def redirect_allowed(uri: str, allowed_hosts: set[str]) -> bool:
    """Only Claude's callback hosts (https) and loopback (Claude Code) may receive codes."""
    parsed = urlparse(uri)
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return host in allowed_hosts and parsed.scheme in ("http", "https")
    return parsed.scheme == "https" and host in allowed_hosts


class ConsentRequests:
    """Signs the pending authorization request into the consent URL, so the
    consent page needs no server-side state and its fields can't be tampered with."""

    def __init__(self, secret: str):
        self._s = URLSafeTimedSerializer(secret, salt="tether-mcp-consent")

    def dump(self, payload: dict) -> str:
        return self._s.dumps(payload)

    def load(self, token: str) -> dict | None:
        try:
            return self._s.loads(token, max_age=CONSENT_MAX_AGE)
        except BadSignature:
            return None


class TetherOAuthProvider:
    def __init__(self, get_conn: Callable[[], sqlite3.Connection], *, base_url: str, resource: str,
                 consent: ConsentRequests, redirect_hosts: set[str]):
        self._conn = get_conn
        self.base_url = base_url.rstrip("/")
        self.resource = resource
        self.consent = consent
        self.redirect_hosts = redirect_hosts

    # ---------------------------------------------------------- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = mcp_auth.get_client(self._conn(), client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not redirect_allowed(str(uri), self.redirect_hosts):
                raise RegistrationError("invalid_redirect_uri", f"redirect URI not allowed: {uri}")
        mcp_auth.save_client(self._conn(), client_info.client_id, client_info.model_dump(mode="json"))

    # -------------------------------------------------------- authorize

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", "unknown resource")
        req = self.consent.dump({
            "client_id": client.client_id,
            "client_name": client.client_name or "",
            "redirect_uri": str(params.redirect_uri),
            "explicit": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "state": params.state,
            "scopes": params.scopes or [],
        })
        return f"{self.base_url}{CONSENT_PATH}?{urlencode({'req': req})}"

    def approve(self, req: dict, subject: str) -> str:
        """Called by the consent page once the owner approves. Returns the code."""
        return mcp_auth.create_grant(
            self._conn(), client_id=req["client_id"], subject=subject, scopes=req["scopes"],
            resource=self.resource,
            code_data={"code_challenge": req["code_challenge"], "redirect_uri": req["redirect_uri"],
                       "explicit": req["explicit"]},
        )

    # ------------------------------------------------------------ codes

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        found = mcp_auth.lookup(self._conn(), authorization_code, "code")
        if not found or found["client_id"] != client.client_id:
            return None
        data = found["data"]
        return AuthorizationCode(
            code=authorization_code, scopes=found["scopes"], expires_at=found["expires_at"],
            client_id=found["client_id"], code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"], redirect_uri_provided_explicitly=data["explicit"],
            resource=found["resource"], subject=found["subject"],
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        return self._tokens(authorization_code.code, "code")

    # ----------------------------------------------------------- tokens

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        found = mcp_auth.lookup(self._conn(), refresh_token, "refresh")
        if not found or found["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=found["client_id"], scopes=found["scopes"],
                            expires_at=found["expires_at"], resource=found["resource"], subject=found["subject"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        return self._tokens(refresh_token.token, "refresh")

    def _tokens(self, presented: str, kind: str) -> OAuthToken:
        conn = self._conn()
        found = mcp_auth.lookup(conn, presented, kind)
        if not found:
            raise TokenError("invalid_grant", f"{kind} is invalid or expired")
        try:
            issued = mcp_auth.issue_tokens(conn, found["grant_id"], consume=presented)
        except LookupError as exc:
            raise TokenError("invalid_grant", f"{kind} was already used") from exc
        return OAuthToken(scope=" ".join(found["scopes"]) or None, **issued)

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = mcp_auth.lookup(self._conn(), token, "access")
        if not found or found["client_id"] == mcp_auth.APP_CLIENT_ID:   # app tokens are for /api only
            return None
        return AccessToken(token=token, client_id=found["client_id"], scopes=found["scopes"],
                           expires_at=found["expires_at"], resource=found["resource"], subject=found["subject"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        mcp_auth.revoke(self._conn(), token.token)
