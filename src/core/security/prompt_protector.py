"""System prompt encryption/decryption at rest.

Protects the core AI guidance prompt from casual reading by encrypting
it with a machine-derived key. Uses Fernet symmetric encryption
(AES-128-CBC + HMAC-SHA256) with PBKDF2 key derivation.

This is protection against casual inspection, not a cryptographic
guarantee against determined reverse engineering.

Usage:
    # Encrypt a prompt
    protector = PromptProtector()
    protector.encrypt_prompt("You are Kuro, a helpful assistant...")

    # Load at startup as core prompt (dual-layer architecture)
    core = load_core_prompt()  # Returns "" if no encrypted file

    # Legacy: load with fallback
    prompt = load_system_prompt(fallback="default prompt text")
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.config import get_kuro_home

logger = structlog.get_logger()

# === Constants ===

ENCRYPTED_PROMPT_FILENAME = "system_prompt.enc"
CURRENT_FORMAT_VERSION = 1
_APP_SALT = b"kuro-prompt-protector-v1"
_DEFAULT_KDF_ITERATIONS = 100_000


# === Key Derivation ===


def _get_machine_fingerprint() -> str:
    """Build a deterministic machine fingerprint.

    Uses username + hostname for cross-platform compatibility.
    """
    try:
        username = os.getlogin()
    except OSError:
        username = os.environ.get(
            "USERNAME", os.environ.get("USER", "unknown")
        )
    hostname = socket.gethostname()
    return f"{username}@{hostname}"


def _derive_fernet_key(fingerprint: str | None = None) -> bytes:
    """Derive a Fernet-compatible key from the machine fingerprint.

    Uses PBKDF2-HMAC-SHA256 with a fixed application salt.
    Returns 32-byte URL-safe base64-encoded key suitable for Fernet.
    """
    if fingerprint is None:
        fingerprint = _get_machine_fingerprint()

    raw_key = hashlib.pbkdf2_hmac(
        "sha256",
        fingerprint.encode("utf-8"),
        _APP_SALT,
        iterations=_DEFAULT_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(raw_key)


# === PromptProtector ===


class PromptProtector:
    """Encrypts and decrypts the system prompt at rest.

    Uses Fernet symmetric encryption with a machine-derived key.
    The encrypted prompt is stored at ~/.kuro/system_prompt.enc
    in a JSON envelope format with version control.
    """

    def __init__(self, kuro_home: Path | None = None) -> None:
        self._home = kuro_home or get_kuro_home()
        self._enc_path = self._home / ENCRYPTED_PROMPT_FILENAME
        self._fernet = None  # Lazy initialization

    @property
    def encrypted_prompt_path(self) -> Path:
        """Path to the encrypted prompt file."""
        return self._enc_path

    def _get_fernet(self, fingerprint: str | None = None):
        """Lazy-load Fernet with derived key."""
        if self._fernet is None or fingerprint is not None:
            from cryptography.fernet import Fernet

            key = _derive_fernet_key(fingerprint)
            fernet = Fernet(key)
            if fingerprint is None:
                self._fernet = fernet
            return fernet
        return self._fernet

    def has_encrypted_prompt(self) -> bool:
        """Check whether an encrypted prompt file exists."""
        return self._enc_path.is_file()

    def encrypt_prompt(self, plaintext: str) -> Path:
        """Encrypt a system prompt and write to the .enc file.

        Args:
            plaintext: The raw system prompt text to encrypt.

        Returns:
            Path to the encrypted file.
        """
        fernet = self._get_fernet()
        ciphertext = fernet.encrypt(plaintext.encode("utf-8"))

        envelope = {
            "version": CURRENT_FORMAT_VERSION,
            "algorithm": "fernet",
            "kdf": "pbkdf2-sha256",
            "kdf_iterations": _DEFAULT_KDF_ITERATIONS,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ciphertext": ciphertext.decode("ascii"),
        }

        self._enc_path.parent.mkdir(parents=True, exist_ok=True)
        self._enc_path.write_text(
            json.dumps(envelope, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

        logger.info(
            "prompt_encrypted",
            path=str(self._enc_path),
            prompt_length=len(plaintext),
        )
        return self._enc_path

    def decrypt_prompt(self) -> str | None:
        """Decrypt the system prompt from the .enc file.

        Returns:
            The decrypted prompt text, or None if decryption fails.
        """
        if not self.has_encrypted_prompt():
            return None

        try:
            raw = json.loads(self._enc_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("prompt_file_read_error", error=str(e))
            return None

        version = raw.get("version", 0)
        if version != CURRENT_FORMAT_VERSION:
            logger.error(
                "prompt_version_unsupported",
                version=version,
                supported=CURRENT_FORMAT_VERSION,
            )
            return None

        ciphertext = raw.get("ciphertext", "")
        if not ciphertext:
            logger.error("prompt_empty_ciphertext")
            return None

        try:
            fernet = self._get_fernet()
            plaintext_bytes = fernet.decrypt(ciphertext.encode("ascii"))
            logger.info("prompt_decrypted", path=str(self._enc_path))
            return plaintext_bytes.decode("utf-8")
        except Exception as e:
            logger.error(
                "prompt_decrypt_failed",
                error=str(e),
                hint="Machine fingerprint may have changed",
            )
            return None


# === Public API ===


def load_system_prompt(
    fallback: str,
    kuro_home: Path | None = None,
) -> str:
    """Load the system prompt, preferring encrypted file over fallback.

    This is the main entry point called by config loading.

    Priority:
        1. Encrypted prompt at ~/.kuro/system_prompt.enc
        2. The provided fallback (default from KuroConfig.system_prompt)

    Args:
        fallback: Default prompt text if no encrypted file exists.
        kuro_home: Override for the ~/.kuro directory.

    Returns:
        The system prompt text (decrypted or fallback).
    """
    protector = PromptProtector(kuro_home)

    if protector.has_encrypted_prompt():
        decrypted = protector.decrypt_prompt()
        if decrypted is not None:
            return decrypted
        logger.warning(
            "prompt_fallback_to_default",
            reason="Encrypted prompt exists but could not be decrypted",
        )

    return fallback


def load_core_prompt(kuro_home: Path | None = None) -> str:
    """Load the encrypted core prompt as a mandatory base layer.

    Unlike load_system_prompt(), this returns "" (empty) when no
    encrypted file is found — there is no fallback. The core prompt
    is always injected as the first SYSTEM message, separate from
    the user-configurable system_prompt.

    Args:
        kuro_home: Override for the ~/.kuro directory.

    Returns:
        The decrypted core prompt, or "" if unavailable.
    """
    protector = PromptProtector(kuro_home)

    if protector.has_encrypted_prompt():
        decrypted = protector.decrypt_prompt()
        if decrypted is not None:
            return decrypted
        logger.warning(
            "core_prompt_decrypt_failed",
            reason="Encrypted prompt exists but could not be decrypted",
        )

    return ""
