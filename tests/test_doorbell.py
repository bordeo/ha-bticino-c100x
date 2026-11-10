import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.doorbell.trigger import DoorbellRangTrigger
from homeassistant.components.event import ATTR_EVENT_TYPE, EventDeviceClass
from homeassistant.core import State

from custom_components.bticino_c100x import manager as manager_mod
from custom_components.bticino_c100x.event import C100XDoorbellEvent
from custom_components.bticino_c100x.manager import C100XManager
from custom_components.bticino_c100x.models import SipAccount
from custom_components.bticino_c100x.sip import SipClient, SipFramer

DOMAIN_GW = "gw123.bs.iotleg.com"


def invite(call_id: str, sender: str = f"c100x@{DOMAIN_GW}") -> bytes:
    sdp = b"v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\n"
    head = (
        f"INVITE sip:user@{DOMAIN_GW} SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 10.0.0.1:5228;branch=z9hG4bK1\r\n"
        f"From: <sip:{sender}>;tag=abc\r\n"
        f"To: <sip:user@{DOMAIN_GW}>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n\r\n"
    ).encode()
    return head + sdp


def make_sip(on_ring):
    account = SipAccount(client_id="c", sip_uri=f"user@{DOMAIN_GW}", sip_password="x", user_oid="o")
    sip = SipClient(account, "c", "k", "ca", on_ring)
    sip.responses = []

    async def respond(msg, status, reason):
        sip.responses.append(status)

    sip._respond = respond
    return sip


def make_manager(loop):
    m = C100XManager.__new__(C100XManager)
    m.hass = MagicMock()
    m.hass.loop = loop
    m.fired = []
    m.hass.bus.async_fire = lambda t, d: m.fired.append((t, d))
    m.entry = MagicMock()
    m.entry.data = {"gateway_id": "gw123"}
    m.ringing = False
    m.last_ring = None
    m._listeners = set()
    m._ring_listeners = set()
    m._seen_call_ids = set()
    m._ring_reset = None
    m.ring_events = []
    m.add_ring_listener(m.ring_events.append)
    return m


async def feed(sip, raw: bytes):
    for msg in SipFramer().feed(raw):
        await sip._handle_request(msg)


@pytest.mark.asyncio
async def test_invite_from_gateway_fires_one_event_and_never_answers():
    m = make_manager(asyncio.get_running_loop())
    sip = make_sip(m._async_ring)
    real_sleep = asyncio.sleep
    with patch("custom_components.bticino_c100x.sip.asyncio.sleep", new=lambda s: real_sleep(0)):
        await feed(sip, invite("call-1"))
        await real_sleep(0.01)
    assert len(m.ring_events) == 1 and m.ring_events[0] == {"call_id": "call-1"}
    assert [t for t, _ in m.fired] == ["bticino_c100x_ring"]
    assert m.ringing is True and isinstance(m.last_ring, datetime)
    # 180 Ringing then 486 Busy (decline so the official app keeps ringing); never 200 OK.
    assert sip.responses == [180, 486]
    m._ring_reset.cancel()


@pytest.mark.asyncio
async def test_retransmitted_invite_same_call_id_is_ignored():
    m = make_manager(asyncio.get_running_loop())
    sip = make_sip(m._async_ring)
    await feed(sip, invite("call-1") + invite("call-1"))
    assert len(m.ring_events) == 1
    m._ring_reset.cancel()


@pytest.mark.asyncio
async def test_new_invite_inside_ring_window_is_debounced_then_next_ring_fires(monkeypatch):
    monkeypatch.setattr(manager_mod, "RING_ACTIVE_SECONDS", 0.05)
    m = make_manager(asyncio.get_running_loop())
    sip = make_sip(m._async_ring)
    await feed(sip, invite("call-1"))
    await feed(sip, invite("call-2"))  # same press, new dialog
    assert len(m.ring_events) == 1 and len(m.fired) == 1
    await asyncio.sleep(0.1)  # window closed
    assert m.ringing is False
    await feed(sip, invite("call-3"))
    assert len(m.ring_events) == 2 and len(m.fired) == 2
    m._ring_reset.cancel()


@pytest.mark.asyncio
async def test_invite_from_other_party_is_not_a_ring():
    m = make_manager(asyncio.get_running_loop())
    sip = make_sip(m._async_ring)
    await feed(sip, invite("call-x", sender="someone@else.example"))
    assert m.ring_events == [] and m.fired == [] and sip.responses == [486]


def test_entity_is_doorbell_and_state_matches_doorbell_rang_trigger():
    entry = MagicMock()
    entry.entry_id = "E1"
    entry.data = {"gateway_id": "gw123"}
    entry.title = "Demo"
    entity = C100XDoorbellEvent(entry)
    assert entity.device_class == EventDeviceClass.DOORBELL
    assert entity.event_types == ["ring"]
    assert entity.unique_id == "E1-doorbell"  # same registry entry -> event.demo_doorbell
    with patch.object(C100XDoorbellEvent, "async_write_ha_state") as write:
        entity._handle_ring({"call_id": "call-1"})
    write.assert_called_once()
    attrs = entity.state_attributes
    assert attrs[ATTR_EVENT_TYPE] == "ring" and attrs["call_id"] == "call-1"
    assert entity.state is not None
    state = State("event.demo_doorbell", entity.state, {**attrs, "device_class": "doorbell"})
    assert DoorbellRangTrigger.is_valid_state(None, state, lambda *a, **k: None) is True
    assert entity.capability_attributes["event_types"] == ["ring"]
