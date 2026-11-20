"""TURN relay selection for remote (CGNAT/cellular) WebRTC viewing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "bticino_c100x"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"bticino_c100x_{name}", PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cfg = _load("webrtc_config")

CF_PAYLOAD = {
    "iceServers": [
        {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
        {
            "urls": [
                "turn:turn.cloudflare.com:3478?transport=udp",
                "turn:turn.cloudflare.com:53?transport=udp",
                "turn:turn.cloudflare.com:3478?transport=tcp",
                "turns:turn.cloudflare.com:443?transport=tcp",
            ],
            "username": "u",
            "credential": "c",
        },
    ]
}


def test_cloudflare_payload_keeps_turn_only_and_drops_port_53() -> None:
    servers = cfg.turn_servers_from_cloudflare(CF_PAYLOAD)
    assert servers == [
        {
            "urls": [
                "turn:turn.cloudflare.com:3478?transport=udp",
                "turn:turn.cloudflare.com:3478?transport=tcp",
                "turns:turn.cloudflare.com:443?transport=tcp",
            ],
            "username": "u",
            "credential": "c",
        }
    ]


def test_aiortc_entry_prefers_udp_and_needs_credentials() -> None:
    stun = SimpleNamespace(urls=["stun:stun.home-assistant.io:3478"], username=None, credential=None)
    turn = SimpleNamespace(
        urls=["turns:turn.cloudflare.com:443?transport=tcp", "turn:turn.cloudflare.com:3478?transport=udp"],
        username="u",
        credential="c",
    )
    assert cfg.aiortc_turn_entry([stun]) is None
    entry = cfg.aiortc_turn_entry([stun, turn])
    assert entry["urls"][0] == "turn:turn.cloudflare.com:3478?transport=udp"


def test_rtc_configuration_adds_relay_for_aiortc() -> None:
    import types

    pkg = types.ModuleType("bticino_pkg")
    pkg.__path__ = [str(PKG)]
    sys.modules["bticino_pkg"] = pkg
    sys.modules["bticino_pkg.webrtc_config"] = cfg
    spec = importlib.util.spec_from_file_location("bticino_pkg.webrtc", PKG / "webrtc.py")
    webrtc = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = webrtc
    spec.loader.exec_module(webrtc)

    plain = webrtc._rtc_configuration()
    assert [s.urls for s in plain.iceServers] == [cfg.SERVER_STUN_URL]
    turn = SimpleNamespace(urls=["turn:turn.cloudflare.com:3478?transport=udp"], username="u", credential="c")
    relayed = webrtc._rtc_configuration([turn])
    assert relayed.iceServers[1].username == "u"
    assert relayed.iceServers[1].urls[0].startswith("turn:")

    # aiortc must accept it and translate it into aioice TURN settings.
    from aiortc.rtcicetransport import connection_kwargs

    kwargs = connection_kwargs(relayed.iceServers)
    assert kwargs["turn_server"] == ("turn.cloudflare.com", 3478)
    assert kwargs["turn_username"] == "u"
    assert kwargs["turn_transport"] == "udp"
