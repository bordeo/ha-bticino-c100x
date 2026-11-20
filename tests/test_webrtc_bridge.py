"""WebRTCBridge peer identity, MediaPlayer refresh, and ICE config."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bticino_c100x.webrtc import WebRTCBridge, rtc_configuration
from custom_components.bticino_c100x.webrtc_config import SERVER_STUN_URL


@pytest.mark.asyncio
async def test_close_ignores_stale_peer_after_session_id_reuse(tmp_path: Path) -> None:
    """A late close() from an overwritten peer must not tear down the new PC."""
    path = tmp_path / "playback.sdp"
    path.write_text("v=0\nm=video 40000 RTP/AVP 96\n")
    bridge = WebRTCBridge(path, viewer_changed=lambda _n: None)

    old = MagicMock()
    old.close = AsyncMock()
    new = MagicMock()
    new.close = AsyncMock()
    bridge._peers["session-1"] = old

    # Simulate answer() replacing the peer for the same HA session_id.
    bridge._peers["session-1"] = new

    await bridge.close("session-1", peer=old)
    old.close.assert_not_awaited()
    assert bridge._peers["session-1"] is new

    await bridge.close("session-1", peer=new)
    new.close.assert_awaited_once()
    assert "session-1" not in bridge._peers


@pytest.mark.asyncio
async def test_ensure_player_force_reopens_same_generation(tmp_path: Path) -> None:
    path = tmp_path / "playback.sdp"
    path.write_text("v=0\nm=video 40000 RTP/AVP 96\n")
    bridge = WebRTCBridge(path, viewer_changed=lambda _n: None)
    first = MagicMock(video=None, audio=None)
    second = MagicMock(video=None, audio=None)
    with patch(
        "custom_components.bticino_c100x.webrtc.MediaPlayer", side_effect=[first, second]
    ):
        await bridge._ensure_player(1, force=False)
        assert bridge._player is first
        await bridge._ensure_player(1, force=True)
        assert bridge._player is second


@pytest.mark.asyncio
async def test_wait_for_player_frame_sets_on_keepalive(tmp_path: Path) -> None:
    path = tmp_path / "playback.sdp"
    path.write_text("v=0\nm=video 40000 RTP/AVP 96\n")
    bridge = WebRTCBridge(path, viewer_changed=lambda _n: None)
    bridge._player_got_frame = False
    bridge._player_frame_event = asyncio.Event()

    async def mark() -> None:
        await asyncio.sleep(0.05)
        bridge._player_got_frame = True
        bridge._player_frame_event.set()

    asyncio.create_task(mark())
    await bridge._wait_for_player_frame_unlocked(1.0)
    assert bridge._player_got_frame is True


def test_rtc_configuration_skips_stun_when_turn_present() -> None:
    from aiortc import RTCIceTransportPolicy

    turn = MagicMock()
    turn.urls = ["turn:example.example:3478?transport=udp"]
    turn.username = "u"
    turn.credential = "p"
    config = rtc_configuration([turn])
    assert len(config.iceServers) == 1
    assert str(config.iceServers[0].urls[0]).startswith("turn:")
    # Host candidates must still be gathered (never iceTransportPolicy=relay).
    assert config.iceTransportPolicy == RTCIceTransportPolicy.ALL


def test_rtc_configuration_no_turn_keeps_lan_stun_and_host_policy() -> None:
    """Alex's rule: TURN is optional; LAN must work with no TURN key."""
    from aiortc import RTCIceTransportPolicy
    from aiortc.rtcicetransport import connection_kwargs

    config = rtc_configuration(None)
    assert len(config.iceServers) == 1
    assert config.iceServers[0].urls == SERVER_STUN_URL
    assert config.iceTransportPolicy == RTCIceTransportPolicy.ALL
    kwargs = connection_kwargs(config.iceServers)
    assert kwargs.get("stun_server") == ("stun.linphone.org", 3478)
    assert "turn_server" not in kwargs


def test_card_ice_gather_timeout_is_short() -> None:
    card = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "bticino_c100x"
        / "frontend"
        / "bticino-c100x-card.js"
    ).read_text(encoding="utf-8")
    assert "ICE_GATHER_TIMEOUT_MS = 2500" in card
