"""Legrand cloud API client."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .auth import C100XAuth
from .const import (
    API_BASE,
    API_SUBSCRIPTION_KEY,
    CERTIFICATE_CLIENT_ID,
    CERTIFICATE_CLIENT_SECRET,
    CERTIFICATE_TENANT_ID,
)
from .models import SipAccount

_LOGGER = logging.getLogger(__name__)

# Path templates only — never interpolate plant/gateway identifiers into logs.
OP_PLANTS = "GET /servicecatalog/api/v3.0/plants"
OP_PLANT = "GET /servicecatalog/api/v3.0/plants/{id}"
OP_MODULES = "GET /servicecatalog/api/v3.0/plants/{id}/modules"
OP_SIP_ACCOUNTS = "GET /vde/sip/v1.0/devices/{id}/sipaccounts"
OP_SIP_REGISTER = "POST /vde/sip/v1.0/devices/{id}/sipaccount"


class ApiError(Exception):
    """A sanitized Legrand API error with diagnosable, non-sensitive metadata."""

    def __init__(
        self,
        status: int,
        operation: str,
        *,
        reason_code: str = "http_error",
        body_kind: str | None = None,
        response_keys: tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.operation = operation
        self.reason_code = reason_code
        self.body_kind = body_kind
        self.response_keys = response_keys
        detail = f"reason={reason_code}"
        if body_kind is not None:
            detail += f" body={body_kind}"
        if response_keys:
            detail += f" keys=[{','.join(response_keys)}]"
        super().__init__(f"Legrand API {operation} failed with HTTP {status} ({detail})")

    def diagnostic_fields(self) -> dict[str, Any]:
        """Fields safe to log or show (no credentials, ids, or payloads)."""
        return {
            "operation": self.operation,
            "http_status": self.status,
            "reason_code": self.reason_code,
            "body_kind": self.body_kind,
            "response_keys": list(self.response_keys),
        }


class LegrandApi:
    """Small API surface required by the integration."""

    def __init__(self, session: aiohttp.ClientSession, auth: C100XAuth) -> None:
        self._session = session
        self._auth = auth

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        operation: str | None = None,
    ) -> Any:
        op = operation or f"{method} {path}"
        for attempt in range(3):
            token = await self._auth.access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "UserToken": token,
                "Ocp-Apim-Subscription-Key": API_SUBSCRIPTION_KEY,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            async with self._session.request(
                method, f"{API_BASE}{path}", headers=headers, json=payload
            ) as response:
                if response.status >= 500 and attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                if response.status >= 400:
                    raise ApiError(
                        response.status,
                        op,
                        reason_code="http_error",
                        body_kind=None,
                    )
                if response.status == 204:
                    return None
                try:
                    return await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError, TypeError) as err:
                    raise ApiError(
                        response.status,
                        op,
                        reason_code="unparseable_json",
                        body_kind="unparseable",
                    ) from err
        raise ApiError(503, op, reason_code="http_error", body_kind=None)

    async def plants(self) -> list[dict]:
        # Original code accepted a bare list or object keys plants|value.
        value = await self._request("GET", "/servicecatalog/api/v3.0/plants", operation=OP_PLANTS)
        return _expect_list(value, "plants", "value", operation=OP_PLANTS)

    async def plant(self, plant_id: str) -> dict:
        # Original: value.get("plant", value) when value is a mapping.
        value = await self._request(
            "GET",
            f"/servicecatalog/api/v3.0/plants/{plant_id}",
            operation=OP_PLANT,
        )
        if isinstance(value, dict):
            plant = value.get("plant", value)
            if isinstance(plant, dict):
                return plant
            raise ApiError(
                502,
                OP_PLANT,
                reason_code="unexpected_object",
                body_kind=_body_kind(value),
                response_keys=_response_keys(value),
            )
        raise ApiError(
            502,
            OP_PLANT,
            reason_code="null_body" if value is None else "invalid_type",
            body_kind=_body_kind(value),
        )

    async def modules(self, plant_id: str) -> list[dict]:
        # Original: bare list or object keys modules|value.
        value = await self._request(
            "GET",
            f"/servicecatalog/api/v3.0/plants/{plant_id}/modules",
            operation=OP_MODULES,
        )
        return _expect_list(value, "modules", "value", operation=OP_MODULES)

    async def sip_accounts(self, gateway_id: str) -> list[SipAccount]:
        # Original iterated the JSON value directly as a list (no wrapper keys).
        value = await self._request(
            "GET",
            f"/vde/sip/v1.0/devices/{gateway_id}/sipaccounts",
            operation=OP_SIP_ACCOUNTS,
        )
        if not isinstance(value, list):
            raise ApiError(
                502,
                OP_SIP_ACCOUNTS,
                reason_code="null_body" if value is None else "invalid_type",
                body_kind=_body_kind(value),
                response_keys=_response_keys(value),
            )
        return [SipAccount.from_api(item) for item in value]

    async def register_sip_account(self, gateway_id: str, client_id: str) -> SipAccount:
        user_oid = await self._auth.user_oid()
        sip_uri = f"{user_oid}_{client_id}@{gateway_id}.bs.iotleg.com"
        value = await self._request(
            "POST",
            f"/vde/sip/v1.0/devices/{gateway_id}/sipaccount",
            {"clientId": client_id, "clientName": "Home Assistant", "sipUri": sip_uri},
            operation=OP_SIP_REGISTER,
        )
        if not isinstance(value, dict):
            raise ApiError(
                502,
                OP_SIP_REGISTER,
                reason_code="null_body" if value is None else "invalid_type",
                body_kind=_body_kind(value),
            )
        return SipAccount.from_api(value)

    async def provision_certificate(self, request: dict) -> dict:
        return await self._certificate_request("POST", "clientCerts", request)

    async def certificate_authority(self) -> dict:
        """Fetch the private CA chain installed by the official app."""
        return await self._certificate_request("GET", "CACerts")

    async def _application_token(self) -> str:
        url = f"https://login.microsoftonline.com/{CERTIFICATE_TENANT_ID}/oauth2/token"
        data = {
            "client_id": CERTIFICATE_CLIENT_ID,
            "client_secret": CERTIFICATE_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
        async with self._session.post(
            url, params={"resource": CERTIFICATE_CLIENT_ID}, data=data
        ) as response:
            try:
                value = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError, TypeError) as err:
                raise ApiError(
                    response.status,
                    "POST certificate application token",
                    reason_code="unparseable_json",
                    body_kind="unparseable",
                ) from err
        if response.status >= 400 or not isinstance(value, dict) or not value.get("access_token"):
            raise ApiError(
                response.status if response.status >= 400 else 502,
                "POST certificate application token",
                reason_code="http_error" if response.status >= 400 else "unexpected_object",
                body_kind=_body_kind(value),
                response_keys=_response_keys(value),
            )
        return str(value["access_token"])

    async def _certificate_request(
        self, method: str, resource: str, payload: dict | None = None
    ) -> dict:
        user_token = await self._auth.access_token()
        application_token = await self._application_token()
        headers = {
            "Authorization": f"Bearer {application_token}",
            "UserToken": user_token,
            "Ocp-Apim-Subscription-Key": API_SUBSCRIPTION_KEY,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        url = f"{API_BASE}/certificate/api/v1.0/ca/information/{resource}"
        async with self._session.request(
            method, url, headers=headers, json=payload
        ) as response:
            try:
                value = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError, TypeError) as err:
                raise ApiError(
                    response.status,
                    f"{method} certificate {resource}",
                    reason_code="unparseable_json",
                    body_kind="unparseable",
                ) from err
        if response.status >= 400:
            raise ApiError(
                response.status,
                f"{method} certificate {resource}",
                reason_code="http_error",
            )
        if not isinstance(value, dict):
            raise ApiError(
                502,
                f"{method} certificate {resource}",
                reason_code="null_body" if value is None else "invalid_type",
                body_kind=_body_kind(value),
            )
        return value


def _body_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _response_keys(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    # Key names only — never values (may contain ids or secrets).
    return tuple(sorted(str(key) for key in value))


def _expect_list(value: Any, *wrapped_keys: str, operation: str) -> list:
    """Parse list payloads using only wrapper keys evidenced in recovered source.

    Evidenced success shapes (from original api.py before hardening):
    - bare JSON array
    - object with one of ``wrapped_keys`` holding an array (plants|modules + value)

    Null / non-mapping / mapping without those list keys → ApiError (no empty
    success). Does not invent alternate envelope keys.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in wrapped_keys:
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
        raise ApiError(
            502,
            operation,
            reason_code="unexpected_object",
            body_kind="object",
            response_keys=_response_keys(value),
        )
    raise ApiError(
        502,
        operation,
        reason_code="null_body" if value is None else "invalid_type",
        body_kind=_body_kind(value),
    )


def log_api_failure(error: ApiError, *, where: str) -> None:
    """Emit one WARNING line with diagnosable fields only."""
    fields = error.diagnostic_fields()
    _LOGGER.warning(
        "BTicino API failure at %s: operation=%s http_status=%s reason_code=%s body_kind=%s response_keys=%s",
        where,
        fields["operation"],
        fields["http_status"],
        fields["reason_code"],
        fields["body_kind"],
        fields["response_keys"],
    )
