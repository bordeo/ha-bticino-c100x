"""Offline SIP REGISTER refresh + door-release regressions (no network)."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import MagicMock

import pytest

from custom_components.bticino_c100x.manager import C100XManager
from custom_components.bticino_c100x.models import SipAccount
from custom_components.bticino_c100x.sip import (
    SipClient,
    SipError,
    SipMessage,
    SipRegistrationTimeout,
)

DOMAIN_GW = "gw123.bs.iotleg.com"


class FakeWriter:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode()


def _account() -> SipAccount:
    return SipAccount(
        client_id="c",
        sip_uri=f"user@{DOMAIN_GW}",
        sip_password="x",
        user_oid="o",
    )


def _sip() -> SipClient:
    client = SipClient(_account(), "c", "k", "ca", lambda _e: asyncio.sleep(0))
    client._writer = FakeWriter()
    return client


def _parse_outgoing(raw: str) -> list[tuple[str, dict[str, str]]]:
    messages: list[tuple[str, dict[str, str]]] = []
    for block in re.split(r"(?=\b(?:REGISTER|MESSAGE|INVITE|BYE|ACK) )", raw):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        messages.append((lines[0], headers))
    return messages


async def _answer_pending(sip: SipClient, status: int = 200) -> None:
    for (call_id, cseq), future in list(sip._pending.items()):
        if future.done():
            continue
        future.set_result(
            SipMessage(
                f"SIP/2.0 {status} OK",
                {"call-id": call_id, "cseq": cseq, "to": f"<sip:user@{DOMAIN_GW}>"},
            )
        )


async def _timeout_immediately(awaitable, timeout):  # noqa: ANN001
    # Leave the pending future alone; production pops it in ``_request`` finally.
    if asyncio.iscoroutine(awaitable):
        awaitable.close()
    raise TimeoutError


@pytest.mark.asyncio
async def test_register_refresh_reuses_call_id_and_increments_cseq() -> None:
    sip = _sip()
    task = asyncio.create_task(sip.register())
    await asyncio.sleep(0)
    await _answer_pending(sip, 200)
    await task

    task = asyncio.create_task(sip.register())
    await asyncio.sleep(0)
    await _answer_pending(sip, 200)
    await task

    writer: FakeWriter = sip._writer  # type: ignore[assignment]
    registers = [
        headers
        for start, headers in _parse_outgoing(writer.text)
        if start.startswith("REGISTER ")
    ]
    assert len(registers) == 2
    assert registers[0]["call-id"] == registers[1]["call-id"]
    assert registers[0]["from"] == registers[1]["from"]
    cseq0 = int(registers[0]["cseq"].split()[0])
    cseq1 = int(registers[1]["cseq"].split()[0])
    assert cseq1 == cseq0 + 1


@pytest.mark.asyncio
async def test_refresh_timeout_raises_registration_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sip = _sip()
    task = asyncio.create_task(sip.register())
    await asyncio.sleep(0)
    await _answer_pending(sip, 200)
    await task

    async def sleep_zero(_seconds: float) -> None:
        return None

    # One refresh iteration: sleep returns immediately, register times out.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.sleep",
            sleep_zero,
        )
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.wait_for",
            _timeout_immediately,
        )
        with pytest.raises(SipRegistrationTimeout):
            await sip._refresh_registration()
    assert any("registration refresh timed out" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_door_release_does_not_queue_behind_pending_refresh() -> None:
    sip = _sip()
    task = asyncio.create_task(sip.register())
    await asyncio.sleep(0)
    await _answer_pending(sip, 200)
    await task
    writer: FakeWriter = sip._writer  # type: ignore[assignment]
    writer.chunks.clear()

    refresh = asyncio.create_task(sip.register())
    await asyncio.sleep(0)  # REGISTER written, awaiting reply
    assert any(c.startswith(b"REGISTER ") for c in writer.chunks)

    release = asyncio.create_task(sip.release_door("lock-1", "gw123"))
    for _ in range(30):
        if any(c.startswith(b"MESSAGE ") for c in writer.chunks):
            break
        await asyncio.sleep(0)
    assert any(
        c.startswith(b"MESSAGE ") for c in writer.chunks
    ), "door release must send while REGISTER refresh is still pending"

    await _answer_pending(sip, 200)
    await asyncio.gather(refresh, release)


@pytest.mark.asyncio
async def test_release_retries_exactly_once_after_link_drop() -> None:
    manager = C100XManager.__new__(C100XManager)
    manager.entry = MagicMock()
    manager.entry.data = {"gateway_id": "gw123"}
    manager.registered = True
    calls: list[str] = []

    class FlakySip:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail
            self.attempts = 0

        async def release_door(self, lock_id: str, gateway_id: str) -> None:
            self.attempts += 1
            calls.append(lock_id)
            if self.fail:
                raise SipError("SIP connection closed")

    first = FlakySip(fail=True)
    second = FlakySip(fail=False)
    manager._sip = first  # type: ignore[assignment]

    async def release_door_and_drop(lock_id: str, gateway_id: str) -> None:
        first.attempts += 1
        calls.append(lock_id)
        # Mimic supervise marking the link down after close.
        manager.registered = False
        manager._sip = None
        raise SipError("SIP connection closed")

    first.release_door = release_door_and_drop  # type: ignore[method-assign]

    async def restore() -> None:
        await asyncio.sleep(0.05)
        manager._sip = second  # type: ignore[assignment]
        manager.registered = True

    restore_task = asyncio.create_task(restore())
    await manager.async_release("lock-1")
    await restore_task

    assert calls == ["lock-1", "lock-1"]
    assert first.attempts == 1
    assert second.attempts == 1


@pytest.mark.asyncio
async def test_release_does_not_retry_more_than_once() -> None:
    manager = C100XManager.__new__(C100XManager)
    manager.entry = MagicMock()
    manager.entry.data = {"gateway_id": "gw123"}
    manager.registered = True
    attempts = {"n": 0}

    class AlwaysDropSip:
        async def release_door(self, lock_id: str, gateway_id: str) -> None:
            attempts["n"] += 1
            raise SipError("SIP connection closed")

    manager._sip = AlwaysDropSip()  # type: ignore[assignment]

    async def wait_then_same_drop(_timeout: float = 60.0) -> None:
        # Pretend reconnect finished, but the new link still drops the retry.
        manager.registered = True
        manager._sip = AlwaysDropSip()  # type: ignore[assignment]

    manager._async_wait_until_registered = wait_then_same_drop  # type: ignore[method-assign]

    with pytest.raises(SipError, match="connection closed"):
        await manager.async_release("lock-1")

    # Original attempt + exactly one retry — never a third send.
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_release_logs_no_response_on_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sip = _sip()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.bticino_c100x.sip.asyncio.wait_for",
            _timeout_immediately,
        )
        with pytest.raises(TimeoutError):
            await sip.release_door("lock-1", "gw123")
    assert any("door release: no response" in r.message for r in caplog.records)
