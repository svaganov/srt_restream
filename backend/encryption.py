"""Secret encryption helpers using Fernet.

The key is loaded from the SECRETS_KEY environment variable once at import time.
A missing or invalid key will be caught at application startup.
"""
import os

from cryptography.fernet import Fernet


_SECRETS_KEY = os.getenv("SECRETS_KEY")
_fernet = Fernet(_SECRETS_KEY) if _SECRETS_KEY else None


def encrypt(value: str) -> str:
    if _fernet is None:
        raise RuntimeError("SECRETS_KEY is not configured")
    return _fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: str) -> str:
    if _fernet is None:
        raise RuntimeError("SECRETS_KEY is not configured")
    return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")
