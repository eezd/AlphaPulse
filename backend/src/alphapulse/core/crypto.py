"""存库凭证的加解密；密钥只在 .env 中"""

from functools import lru_cache

from cryptography.fernet import Fernet

from alphapulse.core.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().secret_encryption_key)


def encrypt(plain: str) -> bytes:
    return _fernet().encrypt(plain.encode())


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(token).decode()
