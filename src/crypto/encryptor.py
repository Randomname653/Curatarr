"""
ARR Suite LLM - AES-256 Encryption Module (Phase A)

Handles user data encryption with user-specific keys derived from PIN.
"""

import base64
import json
import secrets
from typing import Optional, Dict, Any, Tuple

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Random import get_random_bytes
from Crypto.Protocol.KDF import PBKDF2

from src.config import settings


class AesEncryptor:
    """
    AES-256 encryption with user-specific keys.
    
    Key derivation:
    - User provides PIN (8+ characters)
    - PIN + user_plex_id → PBKDF2 → AES-256 key
    - Key NEVER stored on server
    """
    
    def __init__(self):
        """Initialize encryptor."""
        self.iterations = settings.PBKDF2_ITERATIONS
        self.key_size = settings.AES_KEY_SIZE
        self.block_size = 16  # AES block size
    
    def derive_key(self, pin: str, user_id: str, salt: Optional[bytes] = None) -> Tuple[bytes, bytes]:
        """
        Derive AES-256 key from PIN and user ID.

        Args:
            pin: User's PIN (8+ characters)
            user_id: User's Plex user ID (bound into the password material)
            salt: 16-byte salt; generated randomly if not provided

        Returns:
            (key, salt) — both needed: key for crypto, salt for storage
        """
        if salt is None:
            salt = secrets.token_bytes(16)

        # Bind user_id into the password so keys are user-specific
        password = f"{pin}:{user_id}"

        key = PBKDF2(
            password,
            salt,
            dkLen=self.key_size,
            count=self.iterations,
            hmac_hash_module=SHA256,
        )

        return key, salt
    
    def encrypt(self, data: str, key: bytes, kdf_salt: bytes) -> Dict[str, str]:
        """
        Encrypt data with AES-256 in GCM mode.

        Args:
            data: plaintext string
            key: 32-byte AES-256 key (from derive_key)
            kdf_salt: the salt used for key derivation (stored alongside ciphertext for later decryption)

        Returns:
            Dict with 'ciphertext', 'nonce', 'tag', 'salt' (all base64 encoded)
        """
        nonce = get_random_bytes(12)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(data.encode())

        return {
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "tag": base64.b64encode(tag).decode(),
            "salt": base64.b64encode(kdf_salt).decode(),
        }
    
    def decrypt(self, encrypted_data: Dict[str, str], key: bytes) -> str:
        """
        Decrypt data with AES-256 in GCM mode.
        
        Args:
            encrypted_data: Dict with 'ciphertext', 'nonce', 'tag', and 'salt'
            key: 32-byte AES-256 key
            
        Returns:
            Decrypted plaintext string
        """
        try:
            # Decode base64
            ciphertext = base64.b64decode(encrypted_data["ciphertext"])
            nonce = base64.b64decode(encrypted_data["nonce"])
            tag = base64.b64decode(encrypted_data["tag"])
            salt = base64.b64decode(encrypted_data["salt"])
            
            # Create cipher and decrypt
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
            
            return plaintext.decode()
        except (ValueError, TypeError) as e:
            # Return empty string or raise a custom exception for authentication failure
            # This prevents information leakage through timing or error messages
            raise ValueError("Decryption failed: Invalid authentication tag or corrupted data")
    
    def encrypt_json(self, data: Dict[str, Any], key: bytes, kdf_salt: bytes) -> str:
        """
        Encrypt JSON data.

        Args:
            data: Dictionary to encrypt
            key: 32-byte AES-256 key (from derive_key)
            kdf_salt: the salt used for key derivation

        Returns:
            JSON string containing ciphertext, nonce, tag, and kdf salt
        """
        json_str = json.dumps(data, default=str)
        encrypted = self.encrypt(json_str, key, kdf_salt)
        return json.dumps(encrypted)
    
    def decrypt_json(self, encrypted_str: str, key: bytes) -> Dict[str, Any]:
        """
        Decrypt JSON data.
        
        Args:
            encrypted_str: Base64-encoded encrypted JSON
            key: 32-byte AES-256 key
            
        Returns:
            Decrypted dictionary
        """
        try:
            encrypted_data = json.loads(encrypted_str)
            decrypted_str = self.decrypt(encrypted_data, key)
            return json.loads(decrypted_str)
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError("Decryption failed: Invalid JSON or corrupted data")
