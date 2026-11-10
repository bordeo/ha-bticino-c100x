"""Redacted diagnostics."""

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import C100XConfigEntry

REDACT = {
    "password",
    "username",
    "home_id",
    "gateway_id",
    "lock_ids",
    "access_token",
    "refresh_token",
    "sip_password",
    "client_secret",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: C100XConfigEntry) -> dict:
    manager = entry.runtime_data.manager
    expires = manager.certificate_expires_at
    return {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "sip_registered": manager.registered,
        "certificate_expires_at": expires.isoformat() if expires else None,
        "last_ring": manager.last_ring.isoformat() if manager.last_ring else None,
        "last_error_type": manager.last_error,
        "lock_count": len(manager.lock_ids),
        "camera_count": len(manager.camera_ids),
        "microphone_enabled": bool(
            manager.media_session and manager.media_session.microphone_enabled
        ),
    }
