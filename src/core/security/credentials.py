"""Credential management: OS keychain integration via keyring.

Stores sensitive credentials (API keys, tokens) in the OS-native
secure storage:
- Windows: Windows Credential Manager
- macOS: Keychain
- Linux: Secret Service (GNOME Keyring / KDE Wallet)

Never stores credentials in plaintext config files.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

SERVICE_PREFIX = "kuro"


class CredentialManager:
    """Secure credential storage using the OS keychain."""

    def __init__(self) -> None:
        self._available = True
        try:
            import keyring
            self._keyring = keyring
            # Test that the backend is usable
            backend = keyring.get_keyring()
            logger.info("credentials_backend", backend=type(backend).__name__)
        except Exception as e:
            self._available = False
            logger.warning("credentials_unavailable", error=str(e))

    @property
    def is_available(self) -> bool:
        """Check if the credential manager is usable."""
        return self._available

    def _service_name(self, service: str) -> str:
        """Build the full service name for keyring."""
        return f"{SERVICE_PREFIX}:{service}"

    def store(self, service: str, key: str, value: str) -> bool:
        """Store a credential securely.

        Args:
            service: Service name (e.g., "anthropic", "telegram")
            key: Key name (e.g., "api_key", "bot_token")
            value: The secret value to store

        Returns:
            True if stored successfully, False otherwise.
        """
        if not self._available:
            logger.error("credentials_store_failed", reason="backend unavailable")
            return False

        try:
            self._keyring.set_password(
                self._service_name(service),
                key,
                value,
            )
            logger.info("credential_stored", service=service, key=key)
            return True
        except Exception as e:
            logger.error("credential_store_error", service=service, error=str(e))
            return False

    def retrieve(self, service: str, key: str) -> str | None:
        """Retrieve a credential from secure storage.

        Returns None if not found or unavailable.
        """
        if not self._available:
            return None

        try:
            return self._keyring.get_password(
                self._service_name(service),
                key,
            )
        except Exception as e:
            logger.error("credential_retrieve_error", service=service, error=str(e))
            return None

    def delete(self, service: str, key: str) -> bool:
        """Delete a credential from secure storage."""
        if not self._available:
            return False

        try:
            self._keyring.delete_password(
                self._service_name(service),
                key,
            )
            logger.info("credential_deleted", service=service, key=key)
            return True
        except Exception as e:
            logger.error("credential_delete_error", service=service, error=str(e))
            return False

    def list_services(self) -> list[str]:
        """List known services with stored credentials.

        Note: Not all keyring backends support enumeration.
        Returns a best-effort list based on known services.
        """
        known_services = [
            "anthropic", "openai", "google",
            "telegram", "discord", "line",
            "google_calendar",
        ]
        found = []
        for svc in known_services:
            # Try to check if any key exists for this service
            # We check common key names
            for key in ["api_key", "bot_token", "token", "secret"]:
                val = self.retrieve(svc, key)
                if val is not None:
                    found.append(svc)
                    break
        return found
