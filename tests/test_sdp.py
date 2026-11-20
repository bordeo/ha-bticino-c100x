"""Offline SDP offer/answer tests for Classe 100X monitoring."""

from __future__ import annotations

import base64

import pytest

from custom_components.bticino_c100x.sdp import (
    OFFER_AUDIO_PAYLOADS,
    SRTP_KEY_BYTES,
    SRTP_SUITE,
    SdpError,
    build_monitoring_offer,
    build_receive_sdp,
    explain_answer_audio,
    format_negotiated_audio,
    new_crypto,
    parse_answer,
    select_audio_codec,
)


def test_new_crypto_length_and_suite() -> None:
    crypto = new_crypto()
    assert crypto.suite == SRTP_SUITE
    assert len(crypto.key) == SRTP_KEY_BYTES
    assert len(crypto.inline) > 20


def test_build_monitoring_offer_shape() -> None:
    offer = build_monitoring_offer(
        address="203.0.113.10",
        audio_port=40000,
        video_port=40002,
        device_address="aabbccddeeff",
        advertised_audio_port=51000,
        advertised_audio_rtcp_port=51001,
        advertised_video_port=51002,
        advertised_video_rtcp_port=51003,
    )
    assert "a=DEVADDR:aabbccddeeff" in offer.sdp
    payloads = " ".join(str(pt) for pt in OFFER_AUDIO_PAYLOADS)
    assert f"m=audio 51000 RTP/SAVP {payloads}" in offer.sdp
    assert "a=rtpmap:96 opus/48000/2" in offer.sdp
    assert "a=fmtp:96 useinbandfec=1" in offer.sdp
    assert "a=rtpmap:97 speex/16000" in offer.sdp
    assert "a=rtpmap:98 speex/8000" in offer.sdp
    assert "a=rtpmap:8 PCMA/8000" in offer.sdp
    assert "a=rtpmap:0 PCMU/8000" in offer.sdp
    assert "a=rtpmap:101 telephone-event/48000" in offer.sdp
    assert "a=rtpmap:100 telephone-event/8000" in offer.sdp
    assert "a=ptime:20" in offer.sdp
    assert "a=sendrecv" in offer.sdp
    assert "m=video 51002 RTP/SAVP 96" in offer.sdp
    assert "a=recvonly" in offer.sdp
    assert SRTP_SUITE in offer.sdp
    assert offer.audio_port == 40000
    assert offer.advertised_audio_port == 51000


def _answer(*, audio_m: str, audio_rtpmap: str, audio_fmtp: str = "") -> str:
    audio_key = base64.b64encode(b"\x11" * SRTP_KEY_BYTES).decode()
    video_key = base64.b64encode(b"\x22" * SRTP_KEY_BYTES).decode()
    fmtp = f"a=fmtp:{audio_fmtp}\r\n" if audio_fmtp else ""
    return (
        "v=0\r\n"
        "o=- 1 1 IN IP4 198.51.100.9\r\n"
        "s=-\r\n"
        "c=IN IP4 198.51.100.9\r\n"
        "t=0 0\r\n"
        f"m=audio {audio_m}\r\n"
        f"a=rtpmap:{audio_rtpmap}\r\n"
        f"{fmtp}"
        "a=sendrecv\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{audio_key}\r\n"
        "m=video 6002 RTP/SAVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f\r\n"
        "a=sendonly\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{video_key}\r\n"
    )


def test_parse_answer_speex_8000_field_shape() -> None:
    """Door answers Speex/8000 (PT 98) with vbr=on — proven C100X field shape."""
    offer = build_monitoring_offer(
        address="203.0.113.10",
        audio_port=40000,
        video_port=40002,
        device_address="dev",
    )
    answer = _answer(
        audio_m="6000 RTP/SAVP 98 100",
        audio_rtpmap="98 speex/8000",
        audio_fmtp="98 vbr=on",
    )
    # telephone-event is listed but has no rtpmap in this minimal fixture; add it.
    answer = answer.replace(
        "a=rtpmap:98 speex/8000\r\n",
        "a=rtpmap:98 speex/8000\r\na=rtpmap:100 telephone-event/8000\r\n",
    )
    session = parse_answer(answer, offer)
    codec = select_audio_codec(session.audio)
    assert codec is not None
    assert codec.name.casefold() == "speex"
    assert codec.payload_type == 98
    assert codec.clock_rate == 8000
    assert codec.fmtp == "vbr=on"
    summary = format_negotiated_audio(session)
    assert "speex/8000" in summary
    assert "pt=98" in summary
    receive = build_receive_sdp(session)
    assert "m=audio 40000 RTP/SAVP 98" in receive
    assert "a=rtpmap:98 speex/8000" in receive
    assert "telephone-event" not in receive


def test_parse_answer_opus_and_pcma() -> None:
    offer = build_monitoring_offer(
        address="203.0.113.10",
        audio_port=40000,
        video_port=40002,
        device_address="dev",
    )
    answer = _answer(
        audio_m="6000 RTP/SAVP 96",
        audio_rtpmap="96 opus/48000/2",
        audio_fmtp="96 useinbandfec=1",
    )
    session = parse_answer(answer, offer)
    codec = select_audio_codec(session.audio)
    assert codec is not None
    assert codec.name.casefold() == "opus"
    assert codec.payload_type == 96
    assert codec.channels == 2

    answer_pcma = _answer(audio_m="6000 RTP/SAVP 8", audio_rtpmap="8 PCMA/8000")
    session_pcma = parse_answer(answer_pcma, offer)
    assert select_audio_codec(session_pcma.audio).payload_type == 8


def test_parse_answer_rejects_missing_srtp() -> None:
    offer = build_monitoring_offer(
        address="203.0.113.10",
        audio_port=40000,
        video_port=40002,
        device_address="dev",
    )
    answer = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 198.51.100.9\r\n"
        "c=IN IP4 198.51.100.9\r\n"
        "t=0 0\r\n"
        "m=video 6002 RTP/AVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "a=sendonly\r\n"
    )
    with pytest.raises(SdpError):
        parse_answer(answer, offer)


def test_explain_answer_audio_port_zero() -> None:
    text = (
        "v=0\r\n"
        "m=audio 0 RTP/SAVP 8 0\r\n"
        "a=inactive\r\n"
        "m=video 6002 RTP/SAVP 96\r\n"
    )
    assert "port 0" in explain_answer_audio(text)
