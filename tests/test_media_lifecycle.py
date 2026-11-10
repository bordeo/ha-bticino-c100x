"""Isolated media lifecycle regressions (no SIP/device/live FFmpeg network).

Reproducible commands are documented in artifacts/TEST_RUN.md inside the package.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bticino_c100x.media_runtime import (
    MediaRuntime,
    MediaRuntimeError,
    parse_ffmpeg_progress_out_time_us,
    safe_error_code_for_exception,
)
from custom_components.bticino_c100x.media_session import MediaSession, SessionState
from custom_components.bticino_c100x.webrtc import WebRTCBridge


def _runtime() -> MediaRuntime:
    return MediaRuntime(sip=MagicMock(), ffmpeg_binary="ffmpeg", event_callback=AsyncMock())


def test_zero_rejected() -> None:
    runtime = _runtime()
    runtime.ingest_ffmpeg_progress_line("out_time_us=0")
    assert not runtime._first_video.is_set()


def test_positive_sets_ready() -> None:
    runtime = _runtime()
    runtime.ingest_ffmpeg_progress_line("out_time_us=2000000")
    assert runtime._first_video.is_set()
    assert runtime._progress_out_us == 2_000_000


def test_repeated_ignored() -> None:
    runtime = _runtime()
    runtime.ingest_ffmpeg_progress_line("out_time_us=2000000")
    runtime.ingest_ffmpeg_progress_line("out_time_us=2000000")
    assert runtime._progress_out_us == 2_000_000


def test_regression_ignored() -> None:
    runtime = _runtime()
    runtime.ingest_ffmpeg_progress_line("out_time_us=3000000")
    runtime.ingest_ffmpeg_progress_line("out_time_us=2000000")
    assert runtime._progress_out_us == 3_000_000


def test_out_time_ms_is_legacy_microseconds_not_milliseconds() -> None:
    """Independent review defect: out_time_ms must NOT be scaled by 1000."""
    assert parse_ffmpeg_progress_out_time_us("out_time_ms=2000000") == 2_000_000
    assert parse_ffmpeg_progress_out_time_us("out_time_us=2000000") == 2_000_000
    runtime = _runtime()
    # Synthetic paired fields as emitted by real FFmpeg -progress.
    runtime.ingest_ffmpeg_progress_line("out_time_us=2000000")
    runtime.ingest_ffmpeg_progress_line("out_time_ms=2000000")
    assert runtime._progress_out_us == 2_000_000
    # Next legitimate us progression must still advance (no inflated watermark).
    runtime.ingest_ffmpeg_progress_line("out_time_us=2001000")
    assert runtime._progress_out_us == 2_001_000


def test_alias_scaling_inflates_watermark_is_fixed() -> None:
    """Regression against the beta26 defect reproduced by independent offline review."""
    runtime = _runtime()
    runtime.ingest_ffmpeg_progress_line("out_time_ms=2000000")
    # Broken code would store 2_000_000_000 and suppress later out_time_us=2001000.
    assert runtime._progress_out_us == 2_000_000
    runtime.ingest_ffmpeg_progress_line("out_time_us=2001000")
    assert runtime._progress_out_us == 2_001_000


def test_audio_received_does_not_satisfy_video_readiness() -> None:
    runtime = _runtime()
    runtime._audio_received()
    assert runtime._last_audio > 0
    assert not runtime._first_video.is_set()


def test_safe_error_codes_are_allowlisted_not_raw_bodies() -> None:
    err = MediaRuntimeError("STUN media discovery failed /tmp/secret-path")
    code = safe_error_code_for_exception(err)
    assert code == "stun_failed"
    assert "/tmp" not in code
    assert "secret" not in code


@pytest.mark.asyncio
async def test_session_end_clears_active_but_manager_keeps_history() -> None:
    runtime = MagicMock()
    runtime.set_microphone = AsyncMock()
    runtime.end_session = AsyncMock()
    runtime.start_monitoring = AsyncMock()
    session = MediaSession(runtime, notify=lambda: None)
    await session.start("cam")
    session.handle_event({"event": "error", "code": "video_timeout"})
    assert session.last_error == "video_timeout"
    await session.end()
    assert session.last_error is None
    assert session.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_session_start_error_is_code_not_raw_message() -> None:
    runtime = MagicMock()
    runtime.set_microphone = AsyncMock()

    async def boom(*_a, **_k):
        raise MediaRuntimeError("STUN media discovery failed")

    runtime.start_monitoring = boom
    session = MediaSession(runtime, notify=lambda: None)
    with pytest.raises(MediaRuntimeError):
        await session.start("cam")
    assert session.last_error == "stun_failed"


@pytest.mark.asyncio
async def test_ensure_player_uses_generation_and_retires_peers(tmp_path: Path) -> None:
    path = tmp_path / "playback.sdp"
    path.write_text("v=0\nm=video 40000 RTP/AVP 96\n")
    viewers: list[int] = []
    bridge = WebRTCBridge(path, viewer_changed=viewers.append)
    stale = MagicMock()
    stale.close = AsyncMock()
    bridge._peers["stale"] = stale
    bridge._player = MagicMock(video=None, audio=None)
    bridge._player_generation = 1
    player = MagicMock(video=None, audio=None)
    with patch("custom_components.bticino_c100x.webrtc.MediaPlayer", return_value=player):
        await bridge._ensure_player(2)
    assert bridge._player is player
    assert bridge._player_generation == 2
    assert "stale" not in bridge._peers
    stale.close.assert_awaited()
    assert -1 in viewers


@pytest.mark.asyncio
async def test_same_generation_reuses_player_without_retire(tmp_path: Path) -> None:
    path = tmp_path / "playback.sdp"
    path.write_text("v=0\nm=video 40000 RTP/AVP 96\n")
    bridge = WebRTCBridge(path, viewer_changed=lambda _n: None)
    player = MagicMock(video=None, audio=None)
    bridge._player = player
    bridge._player_generation = 3
    with patch("custom_components.bticino_c100x.webrtc.MediaPlayer") as ctor:
        await bridge._ensure_player(3)
    ctor.assert_not_called()
    assert bridge._player is player


def test_manager_owned_generation_survives_runtime_replacement() -> None:
    """SIP reconnect replaces MediaRuntime; generation must not reset to 1 again."""
    seq = {"n": 0}

    def allocate() -> int:
        seq["n"] += 1
        return seq["n"]

    first = MediaRuntime(
        MagicMock(), "ffmpeg", AsyncMock(), allocate_generation=allocate
    )
    second = MediaRuntime(
        MagicMock(), "ffmpeg", AsyncMock(), allocate_generation=allocate
    )
    assert first._next_media_generation() == 1
    # New runtime object (reconnect) sharing manager allocator — not 1 again.
    assert second._next_media_generation() == 2
    assert second._next_media_generation() == 3


@pytest.mark.asyncio
async def test_bridge_retains_across_runtime_replacement_same_numeric_collision(
    tmp_path: Path,
) -> None:
    """Camera bridge persists; without manager-owned gens, both first Starts are '1'.

    Deterministic repro of the review edge: retained bridge + reset runtime-local
    counter would reuse MediaPlayer. Shared allocator forces a new generation.
    """
    path = tmp_path / "playback.sdp"
    path.write_text("v=0\nm=video 40000 RTP/AVP 96\n")
    bridge = WebRTCBridge(path, viewer_changed=lambda _n: None)

    seq = {"n": 0}

    def allocate() -> int:
        seq["n"] += 1
        return seq["n"]

    rt_a = MediaRuntime(
        MagicMock(), "ffmpeg", AsyncMock(), allocate_generation=allocate
    )
    gen_a = rt_a._next_media_generation()
    assert gen_a == 1
    player_a = MagicMock(video=None, audio=None)
    with patch(
        "custom_components.bticino_c100x.webrtc.MediaPlayer", return_value=player_a
    ):
        await bridge._ensure_player(gen_a)
    assert bridge._player is player_a
    assert bridge._player_generation == 1

    # Runtime B replaces A (SIP reconnect). Local-only counter would also yield 1.
    rt_b = MediaRuntime(
        MagicMock(), "ffmpeg", AsyncMock(), allocate_generation=allocate
    )
    gen_b = rt_b._next_media_generation()
    assert gen_b == 2
    assert gen_b != gen_a

    player_b = MagicMock(video=None, audio=None)
    with patch(
        "custom_components.bticino_c100x.webrtc.MediaPlayer", return_value=player_b
    ) as ctor:
        await bridge._ensure_player(gen_b)
    ctor.assert_called_once()
    assert bridge._player is player_b
    assert bridge._player_generation == 2

    # Contrast: colliding numeric generation 1 would incorrectly reuse player_a.
    with patch("custom_components.bticino_c100x.webrtc.MediaPlayer") as ctor2:
        await bridge._ensure_player(2)  # same as current — reuse
    ctor2.assert_not_called()
    assert bridge._player is player_b


@pytest.mark.asyncio
async def test_local_fallback_generation_resets_per_runtime_demonstrates_bug_class() -> None:
    """Without allocate_generation, two runtimes both emit 1 (the review defect class)."""
    a = MediaRuntime(MagicMock(), "ffmpeg", AsyncMock())
    b = MediaRuntime(MagicMock(), "ffmpeg", AsyncMock())
    assert a._next_media_generation() == 1
    assert b._next_media_generation() == 1  # collision if bridge retained


def test_card_end_invalidates_pending_start_token() -> None:
    card = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "bticino_c100x"
        / "frontend"
        / "bticino-c100x-card.js"
    ).read_text(encoding="utf-8")
    assert "actionGeneration" in card
    assert "token !== this.actionGeneration" in card
    # Card uses a dedicated end() that bumps actionGeneration before teardown.
    end_block = card.split("async end()", 1)[1].split("async toggleMicrophone", 1)[0]
    assert "++this.actionGeneration" in end_block
    assert "hideStream" in end_block
    assert "end_entity" in end_block


def test_card_deferred_start_after_end_contract() -> None:
    """Source contract: Start awaits service then checks token before showStream."""
    card = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "bticino_c100x"
        / "frontend"
        / "bticino-c100x-card.js"
    ).read_text(encoding="utf-8")
    # After Start's callService, token must be checked before showStream.
    start_tail = card.split("async start()", 1)[1].split("goLive()", 1)[0]
    assert "const token = ++this.actionGeneration" in start_tail
    assert "callService" in start_tail
    assert start_tail.index("token !== this.actionGeneration") < start_tail.index(
        "showStream"
    )


@pytest.mark.asyncio
async def test_deferred_start_token_skips_showstream_after_end() -> None:
    """Deterministic model of Start resolving after End (no browser)."""

    class FakeCard:
        def __init__(self) -> None:
            self.actionGeneration = 0
            self.stream_shown = 0
            self.status = "Listen-only · microphone off"

        async def deferred_start(self, service) -> None:
            token = self.actionGeneration = self.actionGeneration + 1
            await service()
            if token != self.actionGeneration:
                return
            self.stream_shown += 1

        def end(self) -> None:
            self.actionGeneration += 1
            self.status = "Listen-only · microphone off"

    card = FakeCard()
    gate = asyncio.Event()

    async def slow_start():
        await gate.wait()

    task = asyncio.create_task(card.deferred_start(slow_start))
    await asyncio.sleep(0)
    card.end()
    gate.set()
    await task
    assert card.stream_shown == 0
    assert card.status.startswith("Listen-only")
