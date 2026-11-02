"""Encryption of secrets at rest with the master key.

The master key comes from MW_SECRET_KEY_FILE (preferred) or MW_SECRET_KEY. If
neither is set, a key is generated once in the data directory so a fresh
install works; that file must then be kept with any full backup.
"""

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import Settings

log = logging.getLogger(__name__)

GENERATED_KEY_NAME = "secret.key"


class SecretDecryptError(Exception):
    """A stored secret could not be decrypted (wrong or rotated master key)."""


def _derive_fernet_key(material: bytes) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"mediawrangler-secrets-v1")
    return base64.urlsafe_b64encode(hkdf.derive(material))


def load_master_key(settings: Settings) -> bytes:
    if settings.secret_key_file:
        material = settings.secret_key_file.read_bytes().strip()
        if not material:
            raise RuntimeError(f"MW_SECRET_KEY_FILE {settings.secret_key_file} is empty")
        return material
    if settings.secret_key:
        return settings.secret_key.get_secret_value().encode()

    path: Path = settings.data_dir / GENERATED_KEY_NAME
    if not path.exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(48))
        log.warning(
            "No MW_SECRET_KEY_FILE set; generated %s. Keep it safe: connector "
            "secrets cannot be decrypted without it.",
            path,
        )
    return path.read_bytes().strip()


class SecretBox:
    def __init__(self, master_key: bytes):
        self._fernet = Fernet(_derive_fernet_key(master_key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretDecryptError(
                "stored secret cannot be decrypted with this master key"
            ) from exc
