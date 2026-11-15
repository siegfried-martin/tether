"""Encryption for stored credentials. The key comes from the environment only."""

import json

from cryptography.fernet import Fernet


class Vault:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def seal(self, data: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(data).encode())

    def open(self, ciphertext: bytes) -> dict:
        return json.loads(self._fernet.decrypt(ciphertext))


def generate_key() -> str:
    return Fernet.generate_key().decode()
