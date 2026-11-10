"""Offline SIP CRLF keepalive + framer + reconnect backoff tests."""

from __future__ import annotations

import asyncio
import logging

import pytest

from custom_components.bticino_c100x.const import (
    SIP_KEEPALIVE_PING,
    SIP_KEEPALIVE_PONG,
    SIP_KEEPALIVE_SECONDS,
    SIP_KEEPALIVE_TIMEOUT_SECONDS,
    SIP_RECONNECT_BACKOFF,
    SIP_REREGISTER_SECONDS,
)
from custom_components.bticino_c100x.manager import next_reconnect_delay
from custom_components.bticino_c100x.models import SipAccount
from custom_components.bticino_c100x.sip import (
    SipClient,
    SipError,
    SipFramer,
    SipMessage,
    SipRegistrationTimeout,
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


def test_reregister_interval_below_lb_idle_timeout() -> None:
    assert SIP_REREGISTER_SECONDS == 180


@pytest.mark.asyncio
async def test_keepalive_sends_crlfcrlf_every_period() -> None:
    sip = _sip()
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            # Stop after one keepalive cycle's sleep.
            raise asyncio.CancelledError

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.sleep",
            record_sleep,
        )
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
async def test_missing_pong_does_not_reconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legrand/Azure often never answers CRLFCRLF; do not bounce the socket."""
    sip = _sip()
    cycles = {"n": 0}
    real_sleep = asyncio.sleep

    async def sleep_then_maybe_stop(seconds: float) -> None:
        cycles["n"] += 1
        if cycles["n"] > 2:
            raise asyncio.CancelledError
        # Yield via the real sleep so the monkeypatch cannot recurse.
        await real_sleep(0)

    async def timeout_pong(awaitable, timeout):  # noqa: ANN001
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        assert timeout == SIP_KEEPALIVE_TIMEOUT_SECONDS
        raise TimeoutError

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.sleep",
            sleep_then_maybe_stop,
        )
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.wait_for",
            timeout_pong,
        )
        with caplog.at_level(logging.DEBUG, logger="custom_components.bticino_c100x.sip"):
            with pytest.raises(asyncio.CancelledError):
                await sip._keepalive_loop()

    assert cycles["n"] == 3
    assert any("keepalive pong timed out" in r.message for r in caplog.records)
    assert all(
        r.levelno <= logging.DEBUG
        for r in caplog.records
        if "keepalive pong" in r.message
    )
    writer: FakeWriter = sip._writer  # type: ignore[assignment]
    assert writer.chunks == [SIP_KEEPALIVE_PING, SIP_KEEPALIVE_PING]


@pytest.mark.asyncio
async def test_missing_pong_does_not_surface_via_wait_closed() -> None:
    sip = _sip()
    pings = {"n": 0}
    real_sleep = asyncio.sleep

    async def sleep_yield(_seconds: float) -> None:
        await real_sleep(0)

    async def timeout_pong(awaitable, timeout):  # noqa: ANN001
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        pings["n"] += 1
        raise TimeoutError

    async def hang_forever() -> None:
        await asyncio.Event().wait()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.sleep",
            sleep_yield,
        )
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.wait_for",
            timeout_pong,
        )
        sip._keepalive_task = asyncio.create_task(sip._keepalive_loop())
        sip._read_task = asyncio.create_task(hang_forever())
        sip._registration_task = asyncio.create_task(hang_forever())

        wait_task = asyncio.create_task(sip.wait_closed())
        # Two ignored-pong cycles must leave the session up.
        for _ in range(100):
            if pings["n"] >= 2:
                break
            await real_sleep(0)
        assert pings["n"] >= 2
        assert not wait_task.done(), "missing pong must not finish wait_closed"
        assert not sip._keepalive_task.done()
        wait_task.cancel()
        sip._keepalive_task.cancel()
        sip._read_task.cancel()
        sip._registration_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait_task
        await asyncio.gather(
            sip._keepalive_task,
            sip._read_task,
            sip._registration_task,
            return_exceptions=True,
        )

    writer: FakeWriter = sip._writer  # type: ignore[assignment]
    assert writer.chunks.count(SIP_KEEPALIVE_PING) >= 2


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
async def test_socket_eof_surfaces_via_wait_closed() -> None:
    sip = _sip()

    class FakeReader:
        async def read(self, _n: int) -> bytes:
            return b""

    async def hang_forever() -> None:
        await asyncio.Event().wait()

    sip._reader = FakeReader()  # type: ignore[assignment]
    sip._read_task = asyncio.create_task(sip._read_loop())
    sip._registration_task = asyncio.create_task(hang_forever())
    sip._keepalive_task = asyncio.create_task(hang_forever())
    with pytest.raises(SipError, match="closed the connection"):
        await sip.wait_closed()
    sip._registration_task.cancel()
    sip._keepalive_task.cancel()
    await asyncio.gather(
        sip._registration_task, sip._keepalive_task, return_exceptions=True
    )


@pytest.mark.asyncio
async def test_refresh_every_reregister_seconds() -> None:
    sip = _sip()
    sleeps: list[float] = []
    registers = {"n": 0}

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    async def fake_register() -> None:
        registers["n"] += 1

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.sleep",
            record_sleep,
        )
        mp.setattr(sip, "register", fake_register)
        with pytest.raises(asyncio.CancelledError):
            await sip._refresh_registration()

    assert sleeps[0] == SIP_REREGISTER_SECONDS
    assert registers["n"] == 1


@pytest.mark.asyncio
async def test_refresh_timeout_raises_registration_timeout() -> None:
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
        with pytest.raises(SipRegistrationTimeout):
            await sip._refresh_registration()
