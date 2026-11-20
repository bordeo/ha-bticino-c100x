"""Speex silence, talkback format, and downlink gain helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.bticino_c100x.audio_codec import (
    SPEEX_8000_SILENCE_FRAME,
    OpusEncoder,
    apply_gain_s16,
    speex_silence_frame,
    talkback_format_for,
)


def test_speex_8000_silence_frame_canned() -> None:
    assert speex_silence_frame(8000) == SPEEX_8000_SILENCE_FRAME
    assert len(SPEEX_8000_SILENCE_FRAME) == 38
    assert speex_silence_frame(16000) is None


def test_talkback_format_opus_speex_g711() -> None:
    assert talkback_format_for(SimpleNamespace(name="opus", clock_rate=48000)).sample_rate == 48000
    assert talkback_format_for(SimpleNamespace(name="speex", clock_rate=16000)).sample_rate == 16000
    assert talkback_format_for(SimpleNamespace(name="PCMA", clock_rate=8000)).sample_rate == 8000
    assert talkback_format_for(SimpleNamespace(name="speex", clock_rate=8000)).frame_bytes == 320


def test_apply_gain_s16_boosts_and_clips() -> None:
    quiet = (100).to_bytes(2, "little", signed=True) * 4
    boosted = apply_gain_s16(quiet, 12.0)
    sample = int.from_bytes(boosted[:2], "little", signed=True)
    assert sample == int(100 * (10 ** (12.0 / 20.0)))

    loud = (20000).to_bytes(2, "little", signed=True)
    clipped = apply_gain_s16(loud, 12.0)
    assert int.from_bytes(clipped, "little", signed=True) == 32767

    assert apply_gain_s16(quiet, 0.0) == quiet


def test_opus_encoder_silence_and_pcm() -> None:
    pytest.importorskip("opuslib")
    enc = OpusEncoder(sample_rate=48000, channels=1)
    silence = enc.encode_silence()
    assert isinstance(silence, bytes) and len(silence) > 0
    assert len(silence) < 120
    pcm = b"\x00\x01" * enc.frame_samples
    packet = enc.encode(pcm)
    assert isinstance(packet, bytes) and len(packet) > 0
