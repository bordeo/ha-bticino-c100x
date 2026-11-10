"""Test path bootstrap.

When Home Assistant is installed (CI / doorbell tests), only expose
``custom_components`` on ``sys.path``.

When it is not installed, install lightweight stubs so media lifecycle unit
tests can import the package without the full HA runtime.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _module(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__dict__.setdefault("__path__", [])
        sys.modules[name] = module
    return module


def _ha_available() -> bool:
    try:
        import homeassistant.const  # noqa: F401

        return True
    except ImportError:
        return False


def _ensure_custom_components_package(*, execute_init: bool) -> None:
    parent = _module("custom_components")
    parent.__path__ = [str(ROOT / "custom_components")]
    if execute_init:
        return
    # Avoid executing integration __init__ (needs full HA) for offline unit tests.
    pkg = types.ModuleType("custom_components.bticino_c100x")
    pkg.__path__ = [str(ROOT / "custom_components" / "bticino_c100x")]
    pkg.__file__ = str(ROOT / "custom_components" / "bticino_c100x" / "__init__.py")
    pkg.__package__ = "custom_components.bticino_c100x"
    sys.modules["custom_components.bticino_c100x"] = pkg
    parent.bticino_c100x = pkg


def _install_stubs() -> None:
    if "aiohttp" not in sys.modules:
        aiohttp = _module("aiohttp")
        aiohttp.CookieJar = type("CookieJar", (), {})
        aiohttp.ClientSession = type("ClientSession", (), {})

    ha = _module("homeassistant")
    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (), {})

    class _ConfigFlow:
        def __init_subclass__(cls, domain: str | None = None, **kwargs):
            super().__init_subclass__(**kwargs)
            cls.domain = domain

        def async_show_form(
            self,
            step_id,
            data_schema=None,
            errors=None,
            description_placeholders=None,
        ):
            return {
                "type": "form",
                "step_id": step_id,
                "errors": errors or {},
                "description_placeholders": description_placeholders or {},
            }

        def async_abort(self, reason, description_placeholders=None):
            return {
                "type": "abort",
                "reason": reason,
                "description_placeholders": description_placeholders or {},
            }

        def async_create_entry(self, title=None, data=None):
            return {"type": "create_entry", "title": title, "data": data or {}}

    class _OptionsFlow:
        pass

    config_entries.ConfigFlow = _ConfigFlow
    config_entries.OptionsFlow = _OptionsFlow
    config_entries.ConfigFlowResult = dict

    core = _module("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda fn: fn

    const = _module("homeassistant.const")
    const.CONF_PASSWORD = "password"
    const.CONF_USERNAME = "username"
    const.Platform = types.SimpleNamespace(
        BINARY_SENSOR="binary_sensor",
        BUTTON="button",
        CAMERA="camera",
        EVENT="event",
        SENSOR="sensor",
    )

    exceptions = _module("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})

    helpers = _module("homeassistant.helpers")
    storage = _module("homeassistant.helpers.storage")
    storage.Store = type("Store", (), {})
    aiohttp_client = _module("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_create_clientsession = lambda *args, **kwargs: None
    selector = _module("homeassistant.helpers.selector")
    selector.SelectSelector = type("SelectSelector", (), {"__init__": lambda self, *a, **k: None})
    selector.SelectSelectorConfig = type(
        "SelectSelectorConfig", (), {"__init__": lambda self, *a, **k: None}
    )
    selector.SelectOptionDict = dict

    components = _module("homeassistant.components")
    http = _module("homeassistant.components.http")
    http.StaticPathConfig = type("StaticPathConfig", (), {})
    lovelace = _module("homeassistant.components.lovelace")
    lovelace_const = _module("homeassistant.components.lovelace.const")
    lovelace_const.LOVELACE_DATA = "lovelace"
    lovelace_const.MODE_STORAGE = "storage"
    diagnostics = _module("homeassistant.components.diagnostics")
    diagnostics.async_redact_data = lambda data, keys: {
        key: "**REDACTED**" if key in keys else value for key, value in data.items()
    }
    websocket_api = _module("homeassistant.components.websocket_api")
    websocket_api.websocket_command = lambda schema: (lambda fn: fn)
    websocket_api.async_response = lambda fn: fn
    websocket_api.async_register_command = lambda hass, command: None

    camera = _module("homeassistant.components.camera")
    camera.Camera = type("Camera", (), {})
    camera.CameraEntityFeature = types.SimpleNamespace(STREAM=1)
    camera.WebRTCAnswer = type("WebRTCAnswer", (), {"__init__": lambda self, **k: None})
    camera_webrtc = _module("homeassistant.components.camera.webrtc")
    camera_webrtc.WebRTCClientConfiguration = type("WebRTCClientConfiguration", (), {})
    camera_webrtc.WebRTCSendMessage = object

    ha_entity = _module("homeassistant.helpers.entity")
    ha_entity.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")
    entity_platform = _module("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    sensor = _module("homeassistant.components.sensor")
    sensor.SensorDeviceClass = types.SimpleNamespace(TIMESTAMP="timestamp")
    sensor.SensorEntity = type("SensorEntity", (), {})

    binary_sensor = _module("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorDeviceClass = types.SimpleNamespace(OCCUPANCY="occupancy")
    binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})

    button = _module("homeassistant.components.button")
    button.ButtonEntity = type("ButtonEntity", (), {})

    event = _module("homeassistant.components.event")
    event.EventEntity = type("EventEntity", (), {})

    webrtc_models = _module("webrtc_models")
    webrtc_models.RTCConfiguration = type("RTCConfiguration", (), {})
    webrtc_models.RTCIceCandidateInit = type("RTCIceCandidateInit", (), {})
    webrtc_models.RTCIceServer = type("RTCIceServer", (), {})

    voluptuous = _module("voluptuous")
    voluptuous.Schema = type("Schema", (), {"__init__": lambda self, *a, **k: None})
    voluptuous.Required = lambda *a, **k: object()
    voluptuous.In = lambda *a, **k: object()

    ha.config_entries = config_entries
    ha.core = core
    ha.const = const
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components

    _ensure_custom_components_package(execute_init=False)


if _ha_available():
    _ensure_custom_components_package(execute_init=True)
else:
    _install_stubs()
