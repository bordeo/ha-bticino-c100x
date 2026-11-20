"""Optional Cloudflare Realtime TURN relay for viewing from outside the LAN.

Both the Peraga WAN (CGNAT) and a phone on cellular are behind NATs that
usually block a direct host/srflx ICE path, so remote WebRTC needs a relay.
Cloudflare TURN credentials are short-lived: a long-term TURN key (kept in
``configuration.yaml`` under ``bticino_c100x:`` or in ``bticino_turn.yaml``)
mints them, and we cache and refresh them here. The servers are registered
with Home Assistant's web_rtc registry so browsers receive them via the
camera client configuration, and the aiortc side uses them too.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.components.web_rtc import async_register_ice_servers
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.yaml import load_yaml
from webrtc_models import RTCIceServer

from .webrtc_config import turn_servers_from_cloudflare

_LOGGER = logging.getLogger(__name__)

TURN_FILE_NAME = "bticino_turn.yaml"
CONF_TURN_KEY_ID = "cloudflare_turn_key_id"
CONF_TURN_API_TOKEN = "cloudflare_turn_api_token"

CF_GENERATE_URL = (
    "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}"
    "/credentials/generate-ice-servers"
)
TURN_TTL_SECONDS = 24 * 3600
TURN_REFRESH_MARGIN_SECONDS = 6 * 3600


class CloudflareTurn:
    """Cache short-lived Cloudflare TURN credentials for WebRTC viewers."""

    def __init__(self, hass: HomeAssistant, key_id: str, api_token: str) -> None:
        self.hass = hass
        self._key_id = key_id
        self._api_token = api_token
        self._servers: list[dict[str, Any]] = []
        self._expires = 0.0
        self._refreshing = False

    async def async_refresh(self) -> None:
        session = async_get_clientsession(self.hass)
        async with session.post(
            CF_GENERATE_URL.format(key_id=self._key_id),
            headers={"Authorization": f"Bearer {self._api_token}"},
            json={"ttl": TURN_TTL_SECONDS},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 201:
                # Status only: never log the key id, token or credentials.
                raise RuntimeError(f"Cloudflare TURN HTTP {response.status}")
            payload = await response.json()
        servers = turn_servers_from_cloudflare(payload)
        if not servers:
            raise RuntimeError("Cloudflare TURN returned no usable relay")
        self._servers = servers
        self._expires = time.monotonic() + TURN_TTL_SECONDS

    async def _async_background_refresh(self) -> None:
        try:
            await self.async_refresh()
        except Exception as err:  # noqa: BLE001 - keep serving cached creds
            _LOGGER.warning("BTicino TURN refresh failed: %s", err)
        finally:
            self._refreshing = False

    @callback
    def ice_servers(self) -> list[RTCIceServer]:
        """Return cached relays; schedule a refresh before they expire."""
        now = time.monotonic()
        if now > self._expires - TURN_REFRESH_MARGIN_SECONDS and not self._refreshing:
            self._refreshing = True
            self.hass.async_create_background_task(
                self._async_background_refresh(), "bticino_c100x_turn_refresh"
            )
        if now >= self._expires:
            return []
        return [
            RTCIceServer(
                urls=server["urls"],
                username=server["username"],
                credential=server["credential"],
            )
            for server in self._servers
        ]


def _nonempty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _read_turn_file(path: Path) -> dict[str, str] | None:
    """Load TURN credentials from ``bticino_turn.yaml``.

    Returns ``None`` when the file is missing (same as no TURN configured).
    Raises ``ValueError`` when the file exists but is malformed — callers must
    warn without logging key/token values.
    """
    if not path.is_file():
        return None
    try:
        data = load_yaml(path)
    except Exception as err:  # noqa: BLE001 - treat any parse failure as malformed
        raise ValueError(f"unreadable or invalid YAML ({err.__class__.__name__})") from err
    if not isinstance(data, dict):
        raise ValueError("expected a mapping with TURN keys")
    key_id = _nonempty_str(data.get(CONF_TURN_KEY_ID))
    api_token = _nonempty_str(data.get(CONF_TURN_API_TOKEN))
    if key_id is None or api_token is None:
        raise ValueError(
            f"requires non-empty {CONF_TURN_KEY_ID} and {CONF_TURN_API_TOKEN}"
        )
    return {CONF_TURN_KEY_ID: key_id, CONF_TURN_API_TOKEN: api_token}


async def _async_resolve_turn_credentials(
    hass: HomeAssistant, conf: dict[str, Any]
) -> tuple[str, str] | None:
    """Resolve TURN key id + token: YAML block first, then ``bticino_turn.yaml``."""
    key_id = _nonempty_str(conf.get(CONF_TURN_KEY_ID))
    api_token = _nonempty_str(conf.get(CONF_TURN_API_TOKEN))
    if key_id and api_token:
        return key_id, api_token

    path = Path(hass.config.path(TURN_FILE_NAME))
    try:
        file_conf = await hass.async_add_executor_job(_read_turn_file, path)
    except ValueError as err:
        # File present but bad — never log values.
        _LOGGER.warning(
            "BTicino TURN file %s is malformed (%s); continuing without TURN",
            TURN_FILE_NAME,
            err,
        )
        return None
    if file_conf is None:
        return None
    _LOGGER.debug("TURN credentials loaded from %s", TURN_FILE_NAME)
    return file_conf[CONF_TURN_KEY_ID], file_conf[CONF_TURN_API_TOKEN]


async def async_setup_turn(hass: HomeAssistant, conf: dict[str, Any]) -> None:
    """Register Cloudflare TURN when configured; never fail setup."""
    resolved = await _async_resolve_turn_credentials(hass, conf)
    if resolved is None:
        return
    key_id, api_token = resolved
    turn = CloudflareTurn(hass, key_id, api_token)
    try:
        await turn.async_refresh()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("BTicino TURN disabled for now: %s", err)
    async_register_ice_servers(hass, turn.ice_servers)
