"""Google OAuth 2.0: authorization URL, code exchange, token refresh.

One OAuth app serves both sign-in (openid email) and mailbox connection
(gmail.modify + calendar.readonly, offline access).
"""

import base64
import json
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

LOGIN_SCOPES = "openid email"
# calendar.events is granted on every mailbox so future event writes need no reconnect;
# the hub itself writes only to the personal account. gmail.modify already covers drafts.
MAILBOX_SCOPES = " ".join([
    "openid", "email",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
])
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"

VALID_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class AuthError(Exception):
    """The provider rejected our credential (revoked, expired grant, missing scope)."""


@dataclass
class Identity:
    sub: str
    email: str
    email_verified: bool


def authorization_url(*, client_id: str, redirect_uri: str, scopes: str, state: str, offline: bool,
                      login_hint: str | None = None) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "state": state,
    }
    if offline:
        # prompt=consent guarantees a refresh token on reconnect.
        params |= {"access_type": "offline", "prompt": "consent"}
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(http: httpx.AsyncClient, *, client_id: str, client_secret: str,
                        redirect_uri: str, code: str) -> dict:
    resp = await http.post(TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    if resp.status_code != 200:
        raise AuthError(f"token exchange failed: {resp.status_code}")
    return resp.json()


async def refresh(http: httpx.AsyncClient, *, client_id: str, client_secret: str, refresh_token: str) -> dict:
    resp = await http.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    if resp.status_code in (400, 401):
        # invalid_grant: revoked, or the password changed.
        raise AuthError(f"refresh rejected: {resp.json().get('error', resp.status_code)}")
    resp.raise_for_status()
    return resp.json()


def identity_from_id_token(id_token: str, *, client_id: str) -> Identity:
    """Read claims from an ID token received directly from Google's token endpoint.

    The token came over TLS straight from Google in exchange for our client
    secret, so OIDC Core 3.1.3.7 allows TLS to stand in for the signature check.
    The audience, issuer and expiry are still checked.
    """
    payload = id_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims.get("aud") != client_id:
        raise AuthError("ID token audience mismatch")
    if claims.get("iss") not in VALID_ISSUERS:
        raise AuthError("ID token issuer mismatch")
    if claims.get("exp", 0) < time.time():
        raise AuthError("ID token expired")
    return Identity(sub=claims["sub"], email=claims.get("email", "").lower(),
                    email_verified=bool(claims.get("email_verified")))


def secret_from_token_response(token: dict, identity: Identity) -> dict:
    """The JSON stored (encrypted) in credential.ciphertext."""
    expires_at = int(time.time()) + int(token.get("expires_in", 0))
    return {
        "refresh_token": token["refresh_token"],
        "access_token": token["access_token"],
        "expires_at": expires_at,
        "expires_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at)),
        "email": identity.email,
        "sub": identity.sub,
    }
