"""TURN relay selection for remote (CGNAT/cellular) WebRTC viewing."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "bticino_c100x"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"bticino_c100x_{name}", PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cfg = _load("webrtc_config")

# Full HA is present in CI; import the real turn module for credential resolution.
from custom_components.bticino_c100x import turn as turn_mod  # noqa: E402

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


def test_read_turn_file_missing_returns_none(tmp_path: Path) -> None:
    assert turn_mod._read_turn_file(tmp_path / "bticino_turn.yaml") is None


def test_read_turn_file_valid(tmp_path: Path) -> None:
    path = tmp_path / "bticino_turn.yaml"
    path.write_text(
        'cloudflare_turn_key_id: "kid"\ncloudflare_turn_api_token: "tok"\n',
        encoding="utf-8",
    )
    assert turn_mod._read_turn_file(path) == {
        "cloudflare_turn_key_id": "kid",
        "cloudflare_turn_api_token": "tok",
    }


@pytest.mark.parametrize(
    "body",
    [
        "not: a: mapping: [",
        "cloudflare_turn_key_id: kid\n",
        "cloudflare_turn_api_token: tok\n",
        "cloudflare_turn_key_id: ''\ncloudflare_turn_api_token: tok\n",
        "cloudflare_turn_key_id: kid\ncloudflare_turn_api_token: ''\n",
        "- just a list\n",
        "cloudflare_turn_key_id: 123\ncloudflare_turn_api_token: tok\n",
    ],
)
def test_read_turn_file_malformed_raises(tmp_path: Path, body: str) -> None:
    path = tmp_path / "bticino_turn.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        turn_mod._read_turn_file(path)


def _hass_with_config_path(config_dir: Path) -> MagicMock:
    hass = MagicMock()
    hass.config.path = lambda *parts: str(config_dir.joinpath(*parts))

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = _executor
    return hass


@pytest.mark.asyncio
async def test_resolve_prefers_yaml_block_over_file(tmp_path: Path) -> None:
    (tmp_path / "bticino_turn.yaml").write_text(
        'cloudflare_turn_key_id: "file-kid"\ncloudflare_turn_api_token: "file-tok"\n',
        encoding="utf-8",
    )
    hass = _hass_with_config_path(tmp_path)
    resolved = await turn_mod._async_resolve_turn_credentials(
        hass,
        {
            "cloudflare_turn_key_id": "yaml-kid",
            "cloudflare_turn_api_token": "yaml-tok",
        },
    )
    assert resolved == ("yaml-kid", "yaml-tok")


@pytest.mark.asyncio
async def test_resolve_falls_back_to_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "bticino_turn.yaml").write_text(
        'cloudflare_turn_key_id: "file-kid"\ncloudflare_turn_api_token: "file-tok"\n',
        encoding="utf-8",
    )
    hass = _hass_with_config_path(tmp_path)
    with caplog.at_level(logging.DEBUG, logger=turn_mod.__name__):
        resolved = await turn_mod._async_resolve_turn_credentials(hass, {})
    assert resolved == ("file-kid", "file-tok")
    assert "TURN credentials loaded from bticino_turn.yaml" in caplog.text
    assert "file-tok" not in caplog.text
    assert "file-kid" not in caplog.text


@pytest.mark.asyncio
async def test_resolve_missing_file_is_silent(tmp_path: Path) -> None:
    hass = _hass_with_config_path(tmp_path)
    assert await turn_mod._async_resolve_turn_credentials(hass, {}) is None


@pytest.mark.asyncio
async def test_resolve_malformed_file_warns_without_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "bticino_turn.yaml").write_text(
        'cloudflare_turn_key_id: "secret-kid"\n',
        encoding="utf-8",
    )
    hass = _hass_with_config_path(tmp_path)
    with caplog.at_level(logging.WARNING, logger=turn_mod.__name__):
        resolved = await turn_mod._async_resolve_turn_credentials(hass, {})
    assert resolved is None
    assert "malformed" in caplog.text
    assert "secret-kid" not in caplog.text
