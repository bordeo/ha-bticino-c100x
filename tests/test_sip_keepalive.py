"""Offline SIP CRLF keepalive + framer + reconnect backoff tests."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.bticino_c100x.const import (
    SIP_KEEPALIVE_PING,
    SIP_KEEPALIVE_PONG,
    SIP_KEEPALIVE_SECONDS,
    SIP_KEEPALIVE_TIMEOUT_SECONDS,
    SIP_RECONNECT_BACKOFF,
)
from custom_components.bticino_c100x.manager import next_reconnect_delay
from custom_components.bticino_c100x.models import SipAccount
from custom_components.bticino_c100x.sip import (
    SipClient,
    SipFramer,
    SipKeepaliveTimeout,
    SipMessage,
)

DOMAIN_GW = "gw123.bs.iotleg.com"


class FakeWriter:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, name: str):
        return None


def _sip() -> SipClient:
    account = SipAccount(
        client_id="c", sip_uri=f"user@{DOMAIN_GW}", sip_password="x", user_oid="o"
    )
    client = SipClient(account, "c", "k", "ca", lambda _e: asyncio.sleep(0))
    client._writer = FakeWriter()
    client.registered = True
    return client


def test_framer_strips_leading_pong_before_sip_response() -> None:
    framer = SipFramer()
    sip_ok = (
        b"SIP/2.0 200 OK\r\n"
        b"Call-ID: abc\r\n"
        b"CSeq: 1 REGISTER\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    result = framer.feed(SIP_KEEPALIVE_PONG + sip_ok)
    assert result.pongs == 1
    assert result.server_pings == 0
    assert len(result.messages) == 1
    assert result.messages[0].status_code == 200
    assert result.messages[0].headers["call-id"] == "abc"


def test_framer_handles_interleaved_server_ping_and_pong() -> None:
    framer = SipFramer()
    sip_ok = (
        b"SIP/2.0 200 OK\r\n"
        b"Call-ID: xyz\r\n"
        b"CSeq: 2 REGISTER\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    # Server ping, then pong, then a real response.
    result = framer.feed(SIP_KEEPALIVE_PING + SIP_KEEPALIVE_PONG + sip_ok)
    assert result.server_pings == 1
    assert result.pongs == 1
    assert len(result.messages) == 1
    assert result.messages[0].status_code == 200


def test_framer_partial_crlf_waits_for_more() -> None:
    framer = SipFramer()
    result = framer.feed(b"\r")
    assert result.messages == []
    assert result.pongs == 0
    result = framer.feed(b"\n")
    assert result.pongs == 1
    assert result.messages == []


def test_reconnect_backoff_sequence() -> None:
    assert SIP_RECONNECT_BACKOFF == (1, 2, 5, 30)
    assert [next_reconnect_delay(i) for i in range(6)] == [1, 2, 5, 30, 30, 30]


@pytest.mark.asyncio
async def test_keepalive_sends_crlfcrlf_every_period_and_waits_for_pong() -> None:
    sip = _sip()
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            # Stop after one keepalive cycle's sleep + let timeout path not run.
            raise asyncio.CancelledError

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.sleep",
            record_sleep,
        )
        # First sleep (30s) then send ping; provide pong before wait_for.
        original_wait_for = asyncio.wait_for

        async def wait_for_with_pong(awaitable, timeout):  # noqa: ANN001
            assert timeout == SIP_KEEPALIVE_TIMEOUT_SECONDS
            sip._pong_event.set()
            return await original_wait_for(awaitable, timeout=timeout)

        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.wait_for",
            wait_for_with_pong,
        )
        with pytest.raises(asyncio.CancelledError):
            await sip._keepalive_loop()

    writer: FakeWriter = sip._writer  # type: ignore[assignment]
    assert sleeps[0] == SIP_KEEPALIVE_SECONDS
    assert writer.chunks == [SIP_KEEPALIVE_PING]


@pytest.mark.asyncio
async def test_keepalive_timeout_raises_dead_flow() -> None:
    sip = _sip()

    async def sleep_zero(_seconds: float) -> None:
        return None

    async def timeout_immediately(awaitable, timeout):  # noqa: ANN001
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.sleep",
            sleep_zero,
        )
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.wait_for",
            timeout_immediately,
        )
        with pytest.raises(SipKeepaliveTimeout):
            await sip._keepalive_loop()


@pytest.mark.asyncio
async def test_read_loop_sets_pong_and_replies_to_server_ping() -> None:
    sip = _sip()
    chunks = [
        SIP_KEEPALIVE_PONG,
        SIP_KEEPALIVE_PING,
        b"",  # EOF
    ]

    class FakeReader:
        async def read(self, _n: int) -> bytes:
            return chunks.pop(0) if chunks else b""

    sip._reader = FakeReader()  # type: ignore[assignment]
    with pytest.raises(Exception, match="closed the connection"):
        await sip._read_loop()
    assert sip._pong_event.is_set()
    writer: FakeWriter = sip._writer  # type: ignore[assignment]
    assert SIP_KEEPALIVE_PONG in writer.chunks


@pytest.mark.asyncio
async def test_dead_keepalive_surfaces_via_wait_closed() -> None:
    sip = _sip()

    async def sleep_zero(_seconds: float) -> None:
        return None

    async def timeout_immediately(awaitable, timeout):  # noqa: ANN001
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.sleep",
            sleep_zero,
        )
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.wait_for",
            timeout_immediately,
        )
        sip._keepalive_task = asyncio.create_task(sip._keepalive_loop())
        with pytest.raises(SipKeepaliveTimeout):
            await sip.wait_closed()
