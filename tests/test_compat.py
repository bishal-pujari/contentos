"""Tests for the locked-down-environment compatibility shim (no network)."""

from __future__ import annotations

import builtins
import sys
import uuid

from contentos.core import compat


def test_stdlib_uuid7_returns_a_uuid():
    assert isinstance(compat._stdlib_uuid7(), uuid.UUID)


def test_shim_installed_when_native_import_is_blocked(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name in {"uuid_utils", "uuid_utils.compat"}:
            raise ImportError("DLL load failed: blocked by Application Control policy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.delitem(sys.modules, "uuid_utils", raising=False)
    monkeypatch.delitem(sys.modules, "uuid_utils.compat", raising=False)

    assert compat.ensure_uuid7_available() is False
    shim = sys.modules["uuid_utils.compat"]
    assert isinstance(shim.uuid7(), uuid.UUID)
    # The signature langchain uses (timestamp/nanos) must be accepted too.
    assert isinstance(shim.uuid7(timestamp=1, nanos=2), uuid.UUID)
