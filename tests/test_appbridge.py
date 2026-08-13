"""Platform-bridge tests.

These must pass on a runner with no Shapr3D installed, so they test the
dispatch and the contracts rather than the app itself: which backend gets
chosen, that unsupported platforms raise instead of failing obscurely, and
that path translation is inverted correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from shapr3d_mcp import appbridge


def test_detect_platform_matches_host():
    p = appbridge.detect_platform()
    assert p in {"darwin", "windows", "wsl", "linux"}
    if sys.platform == "darwin":
        assert p == "darwin"
    elif sys.platform == "win32":
        assert p == "windows"
    else:
        # WSL is a Linux kernel, so both are valid; the point is that a
        # non-mac, non-Windows host never claims to be one.
        assert p in {"wsl", "linux"}


def test_is_wsl_false_off_linux():
    if not sys.platform.startswith("linux"):
        assert appbridge._is_wsl() is False


def test_status_note_mentions_no_public_api():
    assert "no public API" in appbridge.status_note()


def test_unsupported_platform_raises_rather_than_returning_junk(monkeypatch):
    """On plain Linux the app tools must raise BridgeUnsupported.

    Returning a falsy/empty result instead would let a caller believe the app
    was driven when nothing happened.
    """
    monkeypatch.setattr(appbridge, "PLATFORM", "linux")
    with pytest.raises(appbridge.BridgeUnsupported):
        appbridge.activate()
    with pytest.raises(appbridge.BridgeUnsupported):
        appbridge.open_file(Path("x.step"))
    assert appbridge.is_installed() is False
    assert appbridge.is_running() is False


def test_is_frontmost_returns_none_not_false_when_unknowable(monkeypatch):
    """None and False mean different things: 'cannot tell' vs 'not in front'.

    Collapsing them would make a broken probe indistinguishable from a real
    negative.
    """
    monkeypatch.setattr(appbridge, "PLATFORM", "linux")
    assert appbridge.is_frontmost() is None


def test_host_path_is_identity_off_wsl(monkeypatch, tmp_path):
    monkeypatch.setattr(appbridge, "PLATFORM", "darwin")
    p = tmp_path / "a.step"
    p.write_text("x")
    assert appbridge.to_host_path(p) == str(p)
    assert appbridge.from_host_path(str(p)) == p


@pytest.mark.skipif(
    appbridge.detect_platform() != "wsl", reason="requires WSL interop"
)
def test_wsl_path_roundtrip(tmp_path):
    p = tmp_path / "roundtrip.step"
    p.write_text("x")
    win = appbridge.to_host_path(p)
    assert "\\" in win and win[1:3] == ":\\", f"not a Windows path: {win}"
    # Round-tripping must land on a file that exists, though not necessarily
    # the same path: files outside /mnt are staged onto the Windows drive.
    assert appbridge.from_host_path(win).exists()


@pytest.mark.skipif(
    appbridge.detect_platform() not in {"windows", "wsl"},
    reason="requires Windows or WSL interop",
)
def test_windows_identity_shape():
    """If Shapr3D is installed, the identity must be a real AUMID.

    A hardcoded family name would pass a weaker assertion; this checks the
    value was actually discovered, by requiring the '!AppId' suffix that only
    comes from reading the package manifest.
    """
    ident = appbridge._win_app_identity()
    if ident is None:
        pytest.skip("Shapr3D is not installed on this host")
    aumid, version = ident
    assert "!" in aumid, f"AUMID missing !AppId: {aumid}"
    assert aumid.startswith("Shapr3D.Shapr3D_")
    assert version and version[0].isdigit()
