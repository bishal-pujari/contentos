"""Compatibility shims for locked-down environments.

Some corporate Windows machines run an Application Control policy that blocks unsigned
compiled extensions. ``langchain-core`` and ``langsmith`` import
``uuid_utils.compat.uuid7`` at module load, and ``uuid_utils`` ships a compiled
``_uuid_utils.pyd`` that such a policy can block with
``DLL load failed ... An Application Control policy has blocked this file``. That breaks
*every* langchain import, so the whole pipeline becomes unusable.

When the real package cannot be imported we install a tiny pure-Python
``uuid_utils.compat`` shim backed by the standard library's ``uuid.uuid7`` (Python 3.14+,
falling back to ``uuid4``), so tracing IDs keep working. The shim is only used when the
native extension is genuinely unavailable.
"""

from __future__ import annotations

import contextlib
import sys
import types


def _stdlib_uuid7(timestamp=None, nanos=None):
    """A ``uuid_utils.compat.uuid7``-compatible callable backed by the stdlib."""
    import uuid as _uuid

    factory = getattr(_uuid, "uuid7", None)
    return factory() if factory is not None else _uuid.uuid4()


def ensure_uuid7_available() -> bool:
    """Import the real ``uuid_utils.compat``, or install a stdlib-backed shim.

    Returns ``True`` when the real package is usable and ``False`` when the shim was
    installed in its place.
    """
    with contextlib.suppress(Exception):  # a blocked or unloadable native extension
        import uuid_utils.compat  # noqa: F401

        return True

    root = types.ModuleType("uuid_utils")
    compat = types.ModuleType("uuid_utils.compat")
    compat.uuid7 = _stdlib_uuid7
    root.compat = compat
    sys.modules.setdefault("uuid_utils", root)
    sys.modules["uuid_utils.compat"] = compat
    return False
