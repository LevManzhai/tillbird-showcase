import pytest

from app.credential_encryption import (
    CredentialEncryptionError,
    decrypt,
    encrypt,
    generate_key,
)

KEY = generate_key()


def test_encrypt_then_decrypt_round_trips():
    ciphertext = encrypt("shpat_super_secret_token", KEY)
    assert decrypt(ciphertext, KEY) == "shpat_super_secret_token"


def test_ciphertext_is_bytes_and_not_the_plaintext():
    ciphertext = encrypt("shpat_super_secret_token", KEY)
    assert isinstance(ciphertext, bytes)
    assert b"shpat_super_secret_token" not in ciphertext


def test_decrypt_with_wrong_key_raises():
    ciphertext = encrypt("shpat_super_secret_token", KEY)
    with pytest.raises(CredentialEncryptionError):
        decrypt(ciphertext, generate_key())


def test_decrypt_corrupted_ciphertext_raises():
    ciphertext = encrypt("shpat_super_secret_token", KEY)
    with pytest.raises(CredentialEncryptionError):
        decrypt(ciphertext[:-5] + b"xxxxx", KEY)


def test_encrypt_with_empty_key_raises():
    with pytest.raises(CredentialEncryptionError):
        encrypt("token", "")


def test_decrypt_with_empty_key_raises():
    with pytest.raises(CredentialEncryptionError):
        decrypt(b"anything", "")


def test_generate_key_produces_a_usable_fernet_key():
    key = generate_key()
    ciphertext = encrypt("x", key)
    assert decrypt(ciphertext, key) == "x"


def test_generate_key_produces_distinct_keys():
    assert generate_key() != generate_key()
