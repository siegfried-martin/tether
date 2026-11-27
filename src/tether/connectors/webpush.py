"""Web Push: encrypt a message for one browser and hand it to that browser's push service.

Message encryption is RFC 8291 (aes128gcm, RFC 8188), and the server identifies itself
with VAPID (RFC 8292). Both are built on `cryptography`, so no push library is needed.

The push service (Google's for Chrome and the Android app) only relays ciphertext: it
can't read what is sent. Endpoints are device addresses, so they are never logged.
"""

import base64
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096
MAX_PAYLOAD = 3000          # well inside one record (and the 4 KB push services accept)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """The aes128gcm body for one subscription (salt and sender are for tests)."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("push payload too large")
    receiver_bytes = unb64(p256dh)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_bytes)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    sender_bytes = _public_bytes(sender.public_key())
    shared = sender.exchange(ec.ECDH(), receiver)
    # RFC 8291 section 3.4: mix in the subscription's auth secret, then RFC 8188's key and nonce.
    prk_key = _hmac(unb64(auth), shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + receiver_bytes + sender_bytes + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)   # \x02: the last (only) record
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(sender_bytes)]) + sender_bytes
    return header + ciphertext


@dataclass(frozen=True)
class Vapid:
    """The server's identity to push services: a P-256 key pair and a contact URI."""
    key: ec.EllipticCurvePrivateKey
    subject: str

    @classmethod
    def from_private(cls, private_b64: str, subject: str) -> "Vapid":
        value = int.from_bytes(unb64(private_b64), "big")
        return cls(ec.derive_private_key(value, ec.SECP256R1()), subject)

    @staticmethod
    def generate() -> str:
        """A new private key, as the raw 32 bytes in base64url (what TETHER_VAPID_PRIVATE_KEY holds)."""
        key = ec.generate_private_key(ec.SECP256R1())
        return b64(key.private_numbers().private_value.to_bytes(32, "big"))

    @property
    def public_key(self) -> str:
        """The applicationServerKey browsers subscribe with."""
        return b64(_public_bytes(self.key.public_key()))

    def authorization(self, endpoint: str, *, now: float | None = None) -> str:
        parts = urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600),
                  "sub": self.subject}
        signing = (b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode()) + "."
                   + b64(json.dumps(claims, separators=(",", ":")).encode()))
        r, s = decode_dss_signature(self.key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        token = signing + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={token}, k={self.public_key}"


class Gone(Exception):
    """The push service says this subscription no longer exists (the app was uninstalled, or
    notifications were turned off): forget it."""


async def send(http: httpx.AsyncClient, vapid: Vapid, endpoint: str, p256dh: str, auth: str, message: dict, *,
               ttl: int = 3600, urgency: str = "normal") -> None:
    """Deliver one message. Raises Gone for a dead subscription, httpx errors for anything else."""
    body = encrypt(json.dumps(message, separators=(",", ":")).encode(), p256dh, auth)
    resp = await http.post(endpoint, content=body, timeout=15, headers={
        "Authorization": vapid.authorization(endpoint),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency,
    })
    if resp.status_code in (404, 410):
        raise Gone()
    resp.raise_for_status()
