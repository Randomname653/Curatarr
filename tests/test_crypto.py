import hashlib
import secrets
import pytest
from src.crypto.encryptor import AesEncryptor

def pbkdf2_sha256_reference(password: str, salt: bytes, iterations: int, dklen: int) -> bytes:
    """Reference implementation using hashlib for verification."""
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations, dklen)

def test_derive_key_deterministic():
    """Test that derive_key returns the expected key and salt when salt is provided."""
    encryptor = AesEncryptor()
    pin = "test_pin_123"
    user_id = "user_456"
    salt = b"0123456789abcdef"

    expected_key = pbkdf2_sha256_reference(f"{pin}:{user_id}", salt, encryptor.iterations, encryptor.key_size)

    key, returned_salt = encryptor.derive_key(pin, user_id, salt=salt)

    assert key == expected_key
    assert returned_salt == salt
    assert len(key) == encryptor.key_size

def test_derive_key_generates_random_salt():
    """Test that derive_key generates a random 16-byte salt when none is provided."""
    encryptor = AesEncryptor()
    pin = "test_pin_123"
    user_id = "user_456"

    key, salt = encryptor.derive_key(pin, user_id)

    assert len(salt) == 16
    assert isinstance(salt, bytes)

    # Verify the key is correctly derived from the generated salt
    expected_key = pbkdf2_sha256_reference(f"{pin}:{user_id}", salt, encryptor.iterations, encryptor.key_size)
    assert key == expected_key

def test_derive_key_different_inputs():
    """Test that different inputs result in different keys."""
    encryptor = AesEncryptor()
    salt = b"fixed_salt_16_bytes"

    key1, _ = encryptor.derive_key("pin1", "user1", salt=salt)
    key2, _ = encryptor.derive_key("pin2", "user1", salt=salt)
    key3, _ = encryptor.derive_key("pin1", "user2", salt=salt)

    assert key1 != key2
    assert key1 != key3
    assert key2 != key3

def test_derive_key_consistency():
    """Test that derive_key is deterministic with the same salt."""
    encryptor = AesEncryptor()
    pin = "secret_pin"
    user_id = "user_1"
    salt = b"constant_salt_12"

    key1, _ = encryptor.derive_key(pin, user_id, salt=salt)
    key2, _ = encryptor.derive_key(pin, user_id, salt=salt)

    assert key1 == key2
