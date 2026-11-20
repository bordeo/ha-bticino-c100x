"""Card module URL digest must not block the event loop at registration time."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant.const")

from custom_components import bticino_c100x as pkg  # noqa: E402


@pytest.mark.asyncio
async def test_async_card_module_url_uses_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(pkg, "async_card_module_url"):
        pytest.skip("integration package stubs omit __init__ helpers")

    monkeypatch.setattr(pkg, "_CARD_DIGEST", None, raising=False)
    calls: list[object] = []

    def fake_compute() -> str:
        calls.append("compute")
        return "deadbeefcafebabe"

    async def fake_executor(func, *args):
        calls.append("executor")
        return func(*args)

    monkeypatch.setattr(pkg, "_compute_card_digest", fake_compute)
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=fake_executor)

    url = await pkg.async_card_module_url(hass)
    assert url == f"{pkg.CARD_STATIC_URL}?v=deadbeefcafebabe"
    assert calls == ["executor", "compute"]
    hass.async_add_executor_job.assert_awaited_once()

    calls.clear()
    url2 = await pkg.async_card_module_url(hass)
    assert url2 == url
    assert calls == []
    assert pkg.card_module_url().endswith("deadbeefcafebabe")
