"""Speex silence keepalive uses the canned 8 kHz frame (no live door)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.bticino_c100x.audio_codec import SPEEX_8000_SILENCE_FRAME
from custom_components.bticino_c100x.media_runtime import AudioRtpProtocol
from custom_components.bticino_c100x.sdp import (
    SRTP_KEY_BYTES,
    Codec,
    CryptoAttribute,
    MediaDescription,
    MonitoringOffer,
    NegotiatedSession,
)


def _session_speex() -> NegotiatedSession:
    audio_crypto = CryptoAttribute(1, "AES_CM_128_HMAC_SHA1_80", b"\x11" * SRTP_KEY_BYTES)
    video_crypto = CryptoAttribute(1, "AES_CM_128_HMAC_SHA1_80", b"\x22" * SRTP_KEY_BYTES)
    offer = MonitoringOffer(
        sdp="",
        address="203.0.113.10",
        audio_port=40000,
        video_port=40002,
        advertised_audio_port=40000,
        advertised_video_port=40002,
        audio_crypto=audio_crypto,
        video_crypto=video_crypto,
    )
    audio = MediaDescription(
        kind="audio",
        port=6000,
        protocol="RTP/SAVP",
        connection="198.51.100.9",
        direction="sendrecv",
        codecs=[Codec(98, "speex", 8000, 1, "vbr=on")],
        crypto=CryptoAttribute(1, "AES_CM_128_HMAC_SHA1_80", b"\x33" * SRTP_KEY_BYTES),
    )
    video = MediaDescription(
        kind="video",
        port=6002,
        protocol="RTP/SAVP",
        connection="198.51.100.9",
        direction="sendonly",
        codecs=[Codec(96, "H264", 90000)],
        crypto=CryptoAttribute(1, "AES_CM_128_HMAC_SHA1_80", b"\x44" * SRTP_KEY_BYTES),
    )
    return NegotiatedSession(offer=offer, audio=audio, video=video)


@pytest.mark.asyncio
async def test_speex_send_silence_uses_canned_frame() -> None:
    protocol = AudioRtpProtocol(lambda: None)
    protocol.configure(_session_speex(), gain_db=12.0)
    assert protocol._codec is not None
    assert protocol._codec.name.casefold() == "speex"
    assert protocol._fmt is not None
    assert protocol._fmt.sample_rate == 8000

    sent: list[bytes] = []

    def capture(payload: bytes, *, samples: int) -> None:
        sent.append(payload)

    protocol._send_payload = capture  # type: ignore[method-assign]
    protocol.transport = MagicMock()
    await protocol.send_silence()
    assert sent == [SPEEX_8000_SILENCE_FRAME]

    # Without a Speex encoder, unmute must NOT stop silence (listen would die).
    assert protocol.talkback_encoder_ready is False
    protocol.set_microphone(True)
    await protocol.send_silence()
    assert sent == [SPEEX_8000_SILENCE_FRAME, SPEEX_8000_SILENCE_FRAME]

    # With a live encoder stub, unmuted silence is suppressed for real mic frames.
    protocol._speex = SimpleNamespace(alive=True)  # type: ignore[assignment]
    assert protocol.talkback_encoder_ready is True
    await protocol.send_silence()
    assert sent == [SPEEX_8000_SILENCE_FRAME, SPEEX_8000_SILENCE_FRAME]

    # Dead encoder must resume silence so unmute cannot kill listen.
    protocol._speex = SimpleNamespace(alive=False)  # type: ignore[assignment]
    assert protocol.talkback_encoder_ready is False
    await protocol.send_silence()
    assert sent == [
        SPEEX_8000_SILENCE_FRAME,
        SPEEX_8000_SILENCE_FRAME,
        SPEEX_8000_SILENCE_FRAME,
    ]
