"""Shared platform markers and explicit substitutes for synthetic tests."""
from contextlib import nullcontext
from pathlib import Path
import sys

import pytest


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only API")


@pytest.fixture
def archive_command_lock(monkeypatch):
    """Use a no-op CLI lock on non-Windows for isolated, synthetic archives only.

    Opt in per test. Windows keeps the real NamedMutex; lock contention tests
    never use this fixture. No environment variable or production fallback
    enables archive commands on unsupported platforms.
    """
    if sys.platform != "win32":
        from mailorganizer import cli

        monkeypatch.setattr(cli, "_archive_lock", lambda identity: nullcontext())


@pytest.fixture
def synthetic_base_interpreter(monkeypatch):
    """Keep command construction portable in tests that inject a fake runner.

    These tests never launch the constructed command. Real interpreter discovery
    and process execution remain covered by the Windows-only tests.
    """
    from mailorganizer import launching

    monkeypatch.setattr(
        launching, "base_interpreter", lambda project, *, console=True: Path(sys.executable)
    )
