"""Optional Cloudflare Realtime TURN relay for viewing from outside the LAN.

Both the Peraga WAN (CGNAT) and a phone on cellular are behind NATs that
usually block a direct host/srflx ICE path, so remote WebRTC needs a relay.
Cloudflare TURN credentials are short-lived: a long-term TURN key (kept in
secrets.yaml) mints them, and we cache and refresh them here. The servers are
registered with Home Assistant's web_rtc registry so browsers receive them via
the camera client configuration, and the aiortc side uses them too.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
from homeassistant.components.web_rtc import async_register_ice_servers
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from webrtc_models import RTCIceServer

from .webrtc_config import turn_servers_from_cloudflare

_LOGGER = logging.getLogger(__name__)

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


async def async_setup_turn(hass: HomeAssistant, conf: dict[str, Any]) -> None:
    """Register Cloudflare TURN when configured in YAML; never fail setup."""
    key_id = conf.get("cloudflare_turn_key_id")
    api_token = conf.get("cloudflare_turn_api_token")
    if not key_id or not api_token:
        return
    turn = CloudflareTurn(hass, str(key_id), str(api_token))
    try:
        await turn.async_refresh()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("BTicino TURN disabled for now: %s", err)
    async_register_ice_servers(hass, turn.ice_servers)
