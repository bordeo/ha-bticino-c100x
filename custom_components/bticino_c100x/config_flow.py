"""Configuration flow for BTicino C100X."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import ApiError, LegrandApi, log_api_failure
from .auth import AuthenticationError, C100XAuth, log_auth_failure
from .const import CONF_GATEWAY_ID, CONF_HOME_ID, CONF_LOCK_IDS, DOMAIN
from .topology import button_id, open_address, visible_lock_modules

# Only B2C logon status!=200 is mapped to invalid_auth. Other AuthenticationError
# reason codes are setup/service failures and must not read as wrong password.
_CREDENTIAL_REJECTED = "credentials_rejected"


class C100XConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return C100XOptionsFlow()

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._plants: list[dict] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        schema = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str})
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=schema)
        # Email identifier: strip autofill whitespace. Do not strip password
        # (spaces may be significant). Empty fields are local validation only.
        username = str(user_input[CONF_USERNAME]).strip()
        password = str(user_input[CONF_PASSWORD])
        if not username or not password:
            return self.async_show_form(
                step_id="user",
                data_schema=schema,
                errors={"base": "invalid_auth"},
            )
        session = async_create_clientsession(self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
        auth = C100XAuth(session, username, password)
        try:
            await auth.authenticate()
            self._plants = await LegrandApi(session, auth).plants()
        except AuthenticationError as err:
            log_auth_failure(err, where="config_flow.user")
            if err.reason_code == _CREDENTIAL_REJECTED:
                return self.async_show_form(
                    step_id="user", data_schema=schema, errors={"base": "invalid_auth"}
                )
            return self.async_show_form(
                step_id="user",
                data_schema=schema,
                errors={"base": "cannot_connect"},
                description_placeholders={"reason_code": err.reason_code},
            )
        except ApiError as err:
            log_api_failure(err, where="config_flow.user.plants")
            return self.async_show_form(step_id="user", data_schema=schema, errors={"base": "cannot_connect"})
        except OSError:
            return self.async_show_form(step_id="user", data_schema=schema, errors={"base": "cannot_connect"})
        finally:
            session.detach()
        if not self._plants:
            return self.async_abort(reason="no_homes")
        await self.async_set_unique_id(username.casefold())
        self._abort_if_unique_id_configured()
        self._credentials = {CONF_USERNAME: username, CONF_PASSWORD: password}
        if len(self._plants) == 1:
            return await self._finish(self._plants[0]["id"])
        return await self.async_step_home()

    async def async_step_home(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        if user_input is None:
            return self._show_home()
        return await self._finish(user_input[CONF_HOME_ID])

    def _show_home(
        self,
        *,
        errors: dict[str, str] | None = None,
        description_placeholders: dict[str, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Keep plant list + credentials; never abort solely to re-pick an installation."""
        homes = _home_choices(self._plants)
        schema = vol.Schema({vol.Required(CONF_HOME_ID): vol.In(homes)})
        return self.async_show_form(
            step_id="home",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def _finish(self, home_id: str) -> config_entries.ConfigFlowResult:
        session = async_create_clientsession(self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
        auth = C100XAuth(session, self._credentials[CONF_USERNAME], self._credentials[CONF_PASSWORD])
        api = LegrandApi(session, auth)
        try:
            modules = await api.modules(home_id)
        except AuthenticationError:
            return self.async_abort(reason="cannot_connect")
        except ApiError as err:
            # Null/empty modules for one plant must not destroy the flow: matching the
            # original request does not prove that plant is valid for this account.
            # Return to Select installation so another entry can be tried without reauth.
            log_api_failure(err, where="config_flow._finish.modules")
            return self._show_home(
                errors={"base": "installation_data_invalid"},
                description_placeholders={
                    "http_status": str(err.status),
                    "reason_code": err.reason_code,
                    "body_kind": err.body_kind or "unknown",
                },
            )
        except OSError:
            return self.async_abort(reason="cannot_connect")
        finally:
            session.detach()
        gateway = next((module for module in modules if module.get("device") == "gateway"), None)
        locks = visible_lock_modules(modules)
        if not gateway or not locks:
            # Modules loaded but no C100X gateway/lock — try another plant, keep session state.
            return self._show_home(errors={"base": "unsupported_installation"})
        self._home_id = home_id
        self._gateway_id = gateway["id"]
        self._lock_choices = _lock_choices(locks)
        return await self.async_step_locks()

    async def async_step_locks(self, user_input=None):
        if user_input is None:
            schema = vol.Schema({vol.Required(CONF_LOCK_IDS): _lock_selector(self._lock_choices)})
            return self.async_show_form(step_id="locks", data_schema=schema)
        selected = user_input[CONF_LOCK_IDS]
        if not selected:
            return self.async_show_form(
                step_id="locks",
                data_schema=vol.Schema({vol.Required(CONF_LOCK_IDS): _lock_selector(self._lock_choices)}),
                errors={"base": "select_lock"},
            )
        plant = next(plant for plant in self._plants if plant["id"] == self._home_id)
        return self.async_create_entry(
            title=plant.get("name", "BTicino C100X"),
            data={
                **self._credentials,
                CONF_HOME_ID: self._home_id,
                CONF_GATEWAY_ID: self._gateway_id,
                CONF_LOCK_IDS: selected,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> config_entries.ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="cannot_connect")
        schema = vol.Schema({vol.Required(CONF_PASSWORD): str})
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema)
        session = async_create_clientsession(self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
        auth = C100XAuth(session, entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
        try:
            await auth.authenticate()
        except AuthenticationError as err:
            log_auth_failure(err, where="config_flow.reauth")
            if err.reason_code == _CREDENTIAL_REJECTED:
                return self.async_show_form(
                    step_id="reauth_confirm", data_schema=schema, errors={"base": "invalid_auth"}
                )
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=schema, errors={"base": "cannot_connect"}
            )
        except OSError:
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors={"base": "cannot_connect"})
        finally:
            session.detach()
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
        )
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")


class C100XOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            if not user_input[CONF_LOCK_IDS]:
                return self.async_show_form(step_id="init", data_schema=self._schema(), errors={"base": "select_lock"})
            return self.async_create_entry(data={CONF_LOCK_IDS: user_input[CONF_LOCK_IDS]})
        session = async_create_clientsession(self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
        try:
            auth = C100XAuth(session, self.config_entry.data[CONF_USERNAME], self.config_entry.data[CONF_PASSWORD])
            modules = await LegrandApi(session, auth).modules(self.config_entry.data[CONF_HOME_ID])
        except (AuthenticationError, ApiError, OSError):
            return self.async_abort(reason="cannot_connect")
        finally:
            session.detach()
        self._choices = _lock_choices(visible_lock_modules(modules))
        return self.async_show_form(step_id="init", data_schema=self._schema())

    def _schema(self):
        current = self.config_entry.options.get(CONF_LOCK_IDS, self.config_entry.data[CONF_LOCK_IDS])
        return vol.Schema({vol.Required(CONF_LOCK_IDS, default=current): _lock_selector(self._choices)})


def _home_choices(plants: list[dict]) -> dict[str, str]:
    """Map plant id → label; disambiguate homonymous installation names.

    Choice *values* are always ``plant[\"id\"]`` — the same field modules() uses.
    Disambiguation only changes labels. Optional ``type`` is included when present
    (evidenced in recovered certificate sender.plant and public WWL samples); it is
    not used as an identifier and does not invent alternate id fields.
    """
    def _base_label(plant: dict) -> str:
        plant_id = str(plant["id"])
        name = str(plant.get("name") or "").strip() or plant_id
        plant_type = str(plant.get("type") or "").strip()
        return f"{name} ({plant_type})" if plant_type else name

    labels = [_base_label(plant) for plant in plants]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    # First pass: trailing-id suffix (readable). Second pass: full id when
    # trailing suffixes still collide (demonstrated with shared id tails).
    tentative: dict[str, str] = {}
    for plant in plants:
        plant_id = str(plant["id"])
        label = _base_label(plant)
        if counts[label] > 1:
            suffix = plant_id[-8:] if len(plant_id) > 8 else plant_id
            tentative[plant_id] = f"{label} · {suffix}"
        else:
            tentative[plant_id] = label
    label_ids: dict[str, list[str]] = {}
    for plant_id, label in tentative.items():
        label_ids.setdefault(label, []).append(plant_id)
    choices: dict[str, str] = {}
    for label, plant_ids in label_ids.items():
        if len(plant_ids) == 1:
            choices[plant_ids[0]] = label
            continue
        for plant_id in plant_ids:
            base = _base_label(next(p for p in plants if str(p["id"]) == plant_id))
            choices[plant_id] = f"{base} · {plant_id}"
    return choices


def _lock_choices(modules):
    return {module["id"]: _lock_label(module, index) for index, module in enumerate(modules, 1)}


def _lock_label(module: dict, index: int) -> str:
    """Build a useful, non-sensitive label for a release module."""
    name = str(module.get("name") or "").strip()
    details: list[str] = []
    if address := open_address(module):
        details.append(f"address {address}")
    details.append(f"button {button_id(module)}")
    label = name or f"Door release {index}"
    return f"{label} ({', '.join(details)})" if details else label


def _lock_selector(choices):
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=value, label=label) for value, label in choices.items()],
            multiple=True,
        )
    )
