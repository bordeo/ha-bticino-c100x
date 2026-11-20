"""Vendored libspeex must load first without find_library probes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bticino_c100x import audio_codec


def test_load_libspeex_prefers_vendored_and_caches(tmp_path: Path, monkeypatch) -> None:
    audio_codec.reset_libspeex_cache_for_tests()
    fake_so = tmp_path / "libspeex.so.1"
    fake_so.write_bytes(b"not-a-real-so")

    monkeypatch.setattr(audio_codec, "_vendor_libspeex_path", lambda: fake_so)
    loaded: list[str] = []

    def fake_cdll(name: str):
        loaded.append(str(name))
        return MagicMock(name="libspeex")

    with patch.object(audio_codec.ctypes, "CDLL", side_effect=fake_cdll):
        with patch.object(audio_codec.ctypes.util, "find_library") as find_lib:
            first = audio_codec._load_libspeex()
            second = audio_codec._load_libspeex()
            find_lib.assert_not_called()

    assert loaded == [str(fake_so)]
    assert first is second
    audio_codec.reset_libspeex_cache_for_tests()


def test_load_libspeex_does_not_call_find_library_on_miss(monkeypatch) -> None:
    audio_codec.reset_libspeex_cache_for_tests()
    monkeypatch.setattr(audio_codec, "_vendor_libspeex_path", lambda: None)

    def boom(_name: str):
        raise OSError("missing")

    with patch.object(audio_codec.ctypes, "CDLL", side_effect=boom):
        with patch.object(audio_codec.ctypes.util, "find_library") as find_lib:
            with pytest.raises(OSError, match="libspeex not available"):
                audio_codec._load_libspeex()
            find_lib.assert_not_called()
    audio_codec.reset_libspeex_cache_for_tests()
