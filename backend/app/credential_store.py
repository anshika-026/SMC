"""
credential_store.py

Encryption at rest for third-party credentials SMC keeps in its database (today: the WhatsApp
Business token an organization grants through Meta Embedded Signup, see whatsapp.py).

  SMC_CREDENTIALS_KEY   a Fernet key (urlsafe base64 of 32 random bytes). Generate one with
                        python -m app.manage generate-credentials-key
                        and keep it outside the database and its backups: whoever has both can
                        read the stored tokens. Losing it means reconnecting (tokens can't be read).

Without the key nothing is encrypted or stored; callers get NotConfigured and must not fall back
to plain text.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

ENV = "SMC_CREDENTIALS_KEY"


class NotConfigured(Exception):
    """SMC_CREDENTIALS_KEY is missing or not a valid Fernet key."""


class Undecryptable(Exception):
    """Stored with another key (the key changed) or tampered with."""


def _fernet() -> Fernet:
    key = os.environ.get(ENV, "").strip()
    if not key:
        raise NotConfigured(f"{ENV} is not set on the server")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise NotConfigured(f"{ENV} is not a valid key (generate one with python -m app.manage generate-credentials-key)") from e


def available() -> bool:
    try:
        _fernet()
        return True
    except NotConfigured:
        return False


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise Undecryptable("Stored credentials can't be read with the current SMC_CREDENTIALS_KEY") from e


def generate_key() -> str:
    return Fernet.generate_key().decode()
