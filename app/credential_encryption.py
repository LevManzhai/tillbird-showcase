"""Encrypt-at-rest for OAuth tokens (app.models.ShopifyOAuthCredential) —
thin wrappers around a Fernet key so every call site reads/writes
ciphertext, never plaintext, and the key management lives in one place.

CREDENTIAL_ENCRYPTION_KEY (.env) is the single most sensitive secret in the
system once any client is live via OAuth — generate it once
(Fernet.generate_key()), store it the same careful way as FLASK_SECRET_KEY
(never printed, never committed, never logged), and never rotate it without
first decrypting every existing row under the old key and re-encrypting
under the new one — a naive rotation just locks out every connected client.
"""

from cryptography.fernet import Fernet, InvalidToken


class CredentialEncryptionError(Exception):
    """Raised when CREDENTIAL_ENCRYPTION_KEY is missing/invalid, or a
    ciphertext fails to decrypt (wrong key, or corrupted/tampered data).
    """


def _fernet(key: str) -> Fernet:
    if not key:
        raise CredentialEncryptionError("CREDENTIAL_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionError(f"CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str, key: str) -> bytes:
    """plaintext -> ciphertext, ready to store in a LargeBinary column."""
    return _fernet(key).encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes, key: str) -> str:
    """ciphertext (as stored) -> the original plaintext. Raises
    CredentialEncryptionError on a wrong key or corrupted/tampered data —
    never returns anything silently wrong.
    """
    try:
        return _fernet(key).decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialEncryptionError("could not decrypt credential — wrong key, or corrupted/tampered data") from exc


def generate_key() -> str:
    """A fresh Fernet key, for `python -c "from app.credential_encryption import generate_key; print(generate_key())"`."""
    return Fernet.generate_key().decode("utf-8")
