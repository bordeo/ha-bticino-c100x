"""In-process Speex encoder (ctypes + vendored/system libspeex)."""

from __future__ import annotations

import math
import struct
from unittest.mock import MagicMock

import pytest

from custom_components.bticino_c100x.audio_codec import (
    SPEEX_8000_SILENCE_FRAME,
    SpeexNativeEncoder,
)
from custom_components.bticino_c100x.media_runtime import AudioRtpProtocol
from custom_components.bticino_c100x.sdp import (
    SRTP_KEY_BYTES,
    Codec,
    CryptoAttribute,
    MediaDescription,
    MonitoringOffer,
    NegotiatedSession,
)


def _tone_pcm(*, samples: int = 160, hz: float = 440.0, rate: int = 8000) -> bytes:
    return b"".join(
        struct.pack("<h", int(10000 * math.sin(2 * math.pi * hz * i / rate)))
        for i in range(samples)
    )


def test_speex_native_encodes_tone_not_silence() -> None:
    try:
        enc = SpeexNativeEncoder(sample_rate=8000, quality=8, vbr=True)
    except OSError as err:
        pytest.skip(f"libspeex unavailable in this environment: {err}")
    try:
        tone = enc.encode(_tone_pcm())
        silence = enc.encode_silence()
    finally:
        enc.close()
    assert isinstance(tone, bytes) and len(tone) > 0
    assert isinstance(silence, bytes) and len(silence) > 0
    assert tone != silence
    assert tone != SPEEX_8000_SILENCE_FRAME


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
async def test_unmuted_send_pcm_uses_native_not_canned_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ffmpeg libspeex absent, unmuted mic still produces non-silence Speex."""
    try:
        native = SpeexNativeEncoder(sample_rate=8000, quality=8, vbr=True)
    except OSError as err:
        pytest.skip(f"libspeex unavailable in this environment: {err}")

    protocol = AudioRtpProtocol(lambda: None)
    protocol.configure(_session_speex(), gain_db=12.0)
    protocol.transport = MagicMock()
    # Simulate prepare_encoders success with native only (no ffmpeg).
    protocol._speex_native = native
    protocol._speex = None
    protocol._speex_backend = "native-libspeex"
    protocol._speex_pending = False
    assert protocol.talkback_encoder_ready is True

    sent: list[bytes] = []
    protocol._send_payload = (  # type: ignore[method-assign]
        lambda payload, *, samples: sent.append(payload)
    )

    protocol.set_microphone(True)
    protocol.send_pcm(_tone_pcm())
    assert len(sent) == 1
    assert sent[0] != SPEEX_8000_SILENCE_FRAME
    assert len(sent[0]) > 0

    # Keepalive must not overwrite talkback while native encoder is ready.
    await protocol.send_silence()
    assert sent == [sent[0]]

    native.close()
