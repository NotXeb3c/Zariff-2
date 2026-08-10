"""Security tests for operating-system-only API-key storage."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pilot.security import vault as vault_module
from pilot.security.vault import KeyVault, VaultUnavailableError
from pilot.server import PilotServer


class _SecureBackend:
    priority = 10


class _UnavailableBackend:
    priority = 0


def _vault(monkeypatch: pytest.MonkeyPatch, backend: object) -> KeyVault:
    monkeypatch.setattr("keyring.get_keyring", lambda: backend)
    monkeypatch.setattr(vault_module, "LEGACY_VAULT_FILE", MagicMock(exists=lambda: False))
    return KeyVault(MagicMock())


@pytest.mark.asyncio
async def test_vault_fails_closed_without_secure_backend(monkeypatch):
    vault = _vault(monkeypatch, _UnavailableBackend())

    assert vault.available is False
    assert await vault.get_key("openai") is None
    assert await vault.list_providers() == []

    with pytest.raises(VaultUnavailableError, match="Secure credential storage is unavailable"):
        await vault.store_key("openai", "secret")
    with pytest.raises(VaultUnavailableError, match="Secure credential storage is unavailable"):
        await vault.delete_key("openai")


@pytest.mark.asyncio
async def test_vault_uses_keyring_and_memory_cache(monkeypatch):
    stored: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(
        "keyring.set_password",
        lambda service, provider, key: stored.__setitem__((service, provider), key),
    )
    monkeypatch.setattr(
        "keyring.get_password",
        lambda service, provider: stored.get((service, provider)),
    )
    monkeypatch.setattr(
        "keyring.delete_password",
        lambda service, provider: stored.pop((service, provider), None),
    )
    vault = _vault(monkeypatch, _SecureBackend())

    await vault.store_key("openai", "secret")
    assert vault.available is True
    assert await vault.get_key("openai") == "secret"
    assert await vault.list_providers() == ["openai"]

    vault.clear_cache()
    assert await vault.get_key("openai") == "secret"
    await vault.delete_key("openai")
    assert await vault.get_key("openai") is None


@pytest.mark.asyncio
async def test_legacy_encrypted_file_is_never_read(monkeypatch, caplog):
    legacy = MagicMock()
    legacy.exists.return_value = True
    monkeypatch.setattr(vault_module, "LEGACY_VAULT_FILE", legacy)
    monkeypatch.setattr("keyring.get_keyring", lambda: _UnavailableBackend())

    vault = KeyVault(MagicMock())

    assert vault.available is False
    assert "detected and ignored" in caplog.text
    legacy.read_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_store_api_key_handler_surfaces_unavailable_backend():
    server = PilotServer.__new__(PilotServer)
    server._vault = MagicMock()
    server._vault.store_key = AsyncMock(side_effect=VaultUnavailableError("Secure credential storage is unavailable."))
    server.config = SimpleNamespace(model=SimpleNamespace(cloud_provider="gemini"))

    result = await server._handle_store_api_key(
        {"provider": "gemini", "api_key": "secret"},
        None,
    )

    assert result == {
        "status": "error",
        "message": "Secure credential storage is unavailable.",
        "available": False,
    }


@pytest.mark.asyncio
async def test_list_api_keys_reports_backend_health():
    server = PilotServer.__new__(PilotServer)
    server._vault = MagicMock(available=False, backend_name="")
    server._vault.list_providers = AsyncMock(return_value=[])

    result = await server._handle_list_api_keys({}, None)

    assert result["providers"] == []
    assert result["available"] is False
    assert "cannot be persisted" in result["message"]


@pytest.mark.asyncio
async def test_list_api_keys_surfaces_broken_keyring():
    server = PilotServer.__new__(PilotServer)
    server._vault = MagicMock(available=True, backend_name="BrokenBackend")
    server._vault.list_providers = AsyncMock(side_effect=VaultUnavailableError("Credential store read failed."))

    result = await server._handle_list_api_keys({}, None)

    assert result == {
        "providers": [],
        "available": False,
        "backend": "BrokenBackend",
        "message": "Credential store read failed.",
    }
