"""Platform bridges to the Shapr3D desktop app.

The CAD half of this server (build123d/OpenCascade) is pure Python and runs
anywhere. Only the *app* half is platform-specific, and it is genuinely
different per platform rather than a matter of swapping command names:

    macOS    Shapr3D is a bundle at /Applications/Shapr3D.app, driven with
             `open -a`, AppleScript and `screencapture`.
    Windows  Shapr3D is an MSIX/Store package. There is no launchable path:
             `C:\\Program Files\\WindowsApps` is ACL-locked, so even
             `os.path.exists` on the install location returns False for an
             installed app. It is launched by AppUserModelID through the
             `shell:AppsFolder\\<PackageFamilyName>!<AppId>` moniker.
    WSL      The server runs on Linux but the app is a Windows package, so
             every app call is a Windows call made over WSL interop, and
             every path handed to the app must be translated first.

Each backend exposes the same five operations; unsupported ones raise
``BridgeUnsupported`` rather than failing obscurely deeper down.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

APP_NAME = "Shapr3D"

# Windows package identity. Discovered at runtime rather than hardcoded --
# the package family suffix is per-publisher and the AppId comes from the
# package manifest, so a literal would be a guess that happens to work on
# one machine.
_WIN_PACKAGE = "Shapr3D.Shapr3D"

_TIMEOUT = 20


class BridgeUnsupported(RuntimeError):
    """The requested app operation is not available on this platform."""


# ---------------------------------------------------------------------------
# platform detection
# ---------------------------------------------------------------------------

def _is_wsl() -> bool:
    """True when running under WSL, where Windows interop is reachable.

    Checked in two independent ways because neither alone is reliable:
    WSL_DISTRO_NAME is absent from some service/systemd contexts, and
    /proc/version can be unreadable in minimal containers.
    """
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def detect_platform() -> str:
    """One of: 'darwin', 'windows', 'wsl', 'linux'."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    if _is_wsl():
        return "wsl"
    return "linux"


PLATFORM = detect_platform()


# ---------------------------------------------------------------------------
# shelling out
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=_TIMEOUT, check=check
    )


def _osascript(script: str) -> str:
    proc = _run(["osascript", "-e", script])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout.strip()


def _powershell_exe() -> str:
    """Resolve PowerShell, on Windows or across the WSL interop boundary.

    Windows PowerShell 5.1 (powershell.exe) is used rather than pwsh because
    it ships with every Windows install; the Appx and System.Drawing calls
    below work on both. From WSL the same executable is reached by name
    through interop, so no separate branch is needed.
    """
    for exe in ("powershell.exe", "pwsh.exe", "pwsh"):
        found = shutil.which(exe)
        if found:
            return found
    raise BridgeUnsupported(
        "PowerShell was not found. On WSL this usually means Windows interop "
        "is disabled -- check that /proc/sys/fs/binfmt_misc/WSLInterop exists "
        "and that interop is enabled in /etc/wsl.conf."
    )


def _ps(script: str) -> str:
    """Run a PowerShell script and return stdout, stripped.

    ``-NonInteractive`` matters: without it a prompt inside a packaged-app
    call can block until the tool timeout with no output to explain why.
    """
    proc = _run([
        _powershell_exe(),
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    # WSL interop yields CRLF; callers compare these strings.
    return proc.stdout.replace("\r\n", "\n").strip()


# ---------------------------------------------------------------------------
# path translation (WSL only)
# ---------------------------------------------------------------------------

def to_host_path(path: Path) -> str:
    """Path as the *app* will see it.

    On WSL a Linux path is meaningless to a Windows app. `wslpath -w` maps it
    to a \\\\wsl.localhost\\... UNC path, which packaged apps often cannot
    open, so files living outside /mnt are staged onto the Windows drive
    first. Files already under /mnt/<drive> translate directly and are not
    copied.
    """
    if PLATFORM != "wsl":
        return str(path)
    src = path.resolve()
    if not str(src).startswith("/mnt/"):
        staging = Path("/mnt/c/Users/Public/shapr3d-mcp")
        staging.mkdir(parents=True, exist_ok=True)
        dst = staging / src.name
        shutil.copy2(src, dst)
        src = dst
    proc = _run(["wslpath", "-w", str(src)])
    if proc.returncode != 0:
        raise RuntimeError(f"wslpath failed for {src}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def from_host_path(win_path: str) -> Path:
    """Inverse of :func:`to_host_path`, for files the app or PowerShell wrote."""
    if PLATFORM != "wsl":
        return Path(win_path)
    proc = _run(["wslpath", "-u", win_path])
    if proc.returncode != 0:
        raise RuntimeError(f"wslpath failed for {win_path}: {proc.stderr.strip()}")
    return Path(proc.stdout.strip())


# ---------------------------------------------------------------------------
# Windows package identity
# ---------------------------------------------------------------------------

_PS_FIND_PACKAGE = f"""
$p = Get-AppxPackage -Name '{_WIN_PACKAGE}' -ErrorAction SilentlyContinue |
     Select-Object -First 1
if ($null -eq $p) {{ Write-Output ''; exit 0 }}
$appId = 'App'
try {{
  $m = ($p | Get-AppxPackageManifest -ErrorAction Stop)
  $a = $m.Package.Applications.Application
  if ($a -is [array]) {{ $a = $a[0] }}
  if ($a -and $a.Id) {{ $appId = $a.Id }}
}} catch {{ }}
Write-Output ("{{0}}!{{1}}`t{{2}}" -f $p.PackageFamilyName, $appId, $p.Version)
"""


def _win_app_identity() -> tuple[str, str] | None:
    """Return (AppUserModelID, version), or None when not installed.

    Deliberately does NOT test the install path: WindowsApps is ACL-locked
    and Test-Path there returns False for a perfectly good install, which is
    a probe whose negative carries no information.
    """
    out = _ps(_PS_FIND_PACKAGE)
    if not out:
        return None
    aumid, _, version = out.partition("\t")
    return aumid.strip(), version.strip()


# ---------------------------------------------------------------------------
# the five operations
# ---------------------------------------------------------------------------

def is_installed() -> bool:
    if PLATFORM == "darwin":
        return Path(f"/Applications/{APP_NAME}.app").exists()
    if PLATFORM in ("windows", "wsl"):
        return _win_app_identity() is not None
    return False


def is_running() -> bool:
    if PLATFORM == "darwin":
        return _run(["pgrep", "-x", APP_NAME]).returncode == 0
    if PLATFORM in ("windows", "wsl"):
        return _ps(
            f"@(Get-Process -Name '{APP_NAME}' -ErrorAction SilentlyContinue).Count"
        ) not in ("", "0")
    return False


# ---------------------------------------------------------------------------
# finding the window of a packaged (MSIX/Store) app
# ---------------------------------------------------------------------------
#
# 🔴 This is the part of the port that cannot be derived from the macOS code,
# and the part a naive translation gets silently wrong.
#
# A Store app does NOT own its own top-level window. Measured on Shapr3D
# 26.121.11316.0:
#
#     Shapr3D.exe            PID 56560   MainWindowHandle 0
#     ApplicationFrameHost   PID 34060   HWND 534920  title "Shapr3D"
#
# So `Get-Process -Name Shapr3D | ... MainWindowHandle` is 0 forever, and
# GetForegroundWindow()'s owning process is ApplicationFrameHost, never
# Shapr3D. Code that trusts either one does not error -- it quietly reports
# "no window", falls back to a full-screen grab, and answers `frontmost:
# False` while the app is plainly in front. A wrong answer that looks like a
# working answer.
#
# The window is therefore located by enumerating visible top-level windows and
# accepting either an ApplicationFrameWindow whose title matches the app, or a
# window genuinely owned by the app process (covers a non-packaged install,
# e.g. an enterprise MSI, without a second code path).

_PS_WINDOW_HELPER = f"""
Add-Type -Namespace S3D -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
[DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int p);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder s, int n);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, System.Text.StringBuilder s, int n);
[StructLayout(LayoutKind.Sequential)] public struct RECT {{ public int L, T, R, B; }}
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
'@ -ErrorAction SilentlyContinue

function Get-AppHwnd {{
  $script:found = [IntPtr]::Zero
  $cb = [S3D.Win+EnumWindowsProc]{{ param($h, $l)
    if ($script:found -ne [IntPtr]::Zero) {{ return $true }}
    if (-not [S3D.Win]::IsWindowVisible($h)) {{ return $true }}
    $sb = New-Object System.Text.StringBuilder 512
    [void][S3D.Win]::GetWindowTextW($h, $sb, 512); $title = $sb.ToString()
    $cn = New-Object System.Text.StringBuilder 256
    [void][S3D.Win]::GetClassNameW($h, $cn, 256); $cls = $cn.ToString()
    $wpid = 0; [void][S3D.Win]::GetWindowThreadProcessId($h, [ref]$wpid)
    $pname = (Get-Process -Id $wpid -ErrorAction SilentlyContinue).Name
    if (($cls -eq 'ApplicationFrameWindow' -and $title -eq '{APP_NAME}') -or
        ($pname -eq '{APP_NAME}' -and $title)) {{
      $script:found = $h
    }}
    return $true
  }}
  [void][S3D.Win]::EnumWindows($cb, [IntPtr]::Zero)
  return $script:found
}}
"""

_PS_FRONTMOST = _PS_WINDOW_HELPER + """
$h = Get-AppHwnd
if ($h -ne [IntPtr]::Zero -and $h -eq [S3D.Win]::GetForegroundWindow()) { 'True' } else { 'False' }
"""


def is_frontmost() -> bool | None:
    """True/False, or None when the platform cannot answer."""
    try:
        if PLATFORM == "darwin":
            front = _osascript(
                'tell application "System Events" to get name of first '
                "application process whose frontmost is true"
            )
            return front == APP_NAME
        if PLATFORM in ("windows", "wsl"):
            return _ps(_PS_FRONTMOST) == "True"
    except Exception:
        return None
    return None


def activate() -> str:
    if PLATFORM == "darwin":
        _run(["open", "-a", APP_NAME], check=True)
        return f"{APP_NAME} activated."
    if PLATFORM in ("windows", "wsl"):
        ident = _win_app_identity()
        if ident is None:
            raise BridgeUnsupported(
                f"{APP_NAME} is not installed for this Windows user "
                f"(no '{_WIN_PACKAGE}' package)."
            )
        aumid, _version = ident
        # explorer.exe is the documented way to launch by AppUserModelID; it
        # returns immediately and does not require the package path to be
        # readable.
        _ps(f"Start-Process -FilePath 'shell:AppsFolder\\{aumid}'")
        return f"{APP_NAME} activated (AppUserModelID {aumid})."
    raise BridgeUnsupported(
        f"Launching {APP_NAME} is not supported on platform '{PLATFORM}'."
    )


def open_file(path: Path) -> str:
    """Hand a CAD file to the app.

    On Windows this is materially weaker than the macOS path and the caller
    should say so: .step/.stp carry no file association, so the file is passed
    as a launch argument. If the app ignores it, the user still has to import
    manually -- the file is staged at a Windows-visible path and named in the
    return value precisely so that fallback is one paste, not a hunt.
    """
    if PLATFORM == "darwin":
        _run(["open", "-a", APP_NAME, str(path)], check=True)
        return (
            f"Sent {path.name} to {APP_NAME}. The app shows an Import "
            "Preferences dialog; the user clicks Import. STEP files carry "
            "their units (mm), so no unit choice is needed."
        )
    if PLATFORM in ("windows", "wsl"):
        ident = _win_app_identity()
        if ident is None:
            raise BridgeUnsupported(
                f"{APP_NAME} is not installed for this Windows user."
            )
        aumid, _version = ident
        host = to_host_path(path)
        escaped = host.replace("'", "''")
        _ps(
            f"Start-Process -FilePath 'shell:AppsFolder\\{aumid}' "
            f"-ArgumentList '\"{escaped}\"'"
        )
        return (
            f"Launched {APP_NAME} with {path.name} as an argument.\n"
            f"Windows path: {host}\n"
            "NOTE: .step/.stp have no file association on Windows, so the app "
            "may ignore the argument and simply open. If no import dialog "
            "appears, use Shapr3D's Add > Import and pick the path above."
        )
    raise BridgeUnsupported(
        f"Opening files in {APP_NAME} is not supported on platform '{PLATFORM}'."
    )


_PS_SCREENSHOT = _PS_WINDOW_HELPER + """
Add-Type -AssemblyName System.Drawing, System.Windows.Forms
$hwnd = Get-AppHwnd
$bounds = $null
$scoped = $false
$occluded = $false
if ($hwnd -ne [IntPtr]::Zero) {
  # CopyFromScreen copies SCREEN PIXELS in a rectangle -- not the window's own
  # contents. If another window overlaps the app, that is what lands in the
  # PNG, and nothing about locating the handle detects it. So the window is
  # raised first and the foreground is re-checked afterwards.
  #
  # PrintWindow was the alternative and is worse here: a GPU/Direct3D-rendered
  # viewport, which is exactly what a CAD app has, commonly comes back black.
  if ([S3D.Win]::IsIconic($hwnd)) { [void][S3D.Win]::ShowWindow($hwnd, 9) }  # SW_RESTORE
  [void][S3D.Win]::SetForegroundWindow($hwnd)
  Start-Sleep -Milliseconds 900
  $occluded = ([S3D.Win]::GetForegroundWindow() -ne $hwnd)
  $r = New-Object S3D.Win+RECT
  if ([S3D.Win]::GetWindowRect($hwnd, [ref]$r)) {
    $w = $r.R - $r.L; $h = $r.B - $r.T
    if ($w -gt 0 -and $h -gt 0) {
      $bounds = New-Object System.Drawing.Rectangle $r.L, $r.T, $w, $h
      $scoped = $true
    }
  }
}
if (-not $scoped) { $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds }
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size)
$bmp.Save($OUTFILE, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
# The caller needs to know which of the three it got. Reporting "the app
# window" for a full-screen fallback, or for a rectangle another window was
# sitting on top of, is the silent-wrong-answer case this port already hit
# once: it captured a browser and called it a success.
if (-not $scoped)  { Write-Output 'FULLSCREEN' }
elseif ($occluded) { Write-Output 'OCCLUDED' }
else               { Write-Output 'WINDOW' }
"""


def screenshot(out: Path) -> Path:
    """Capture the app window, falling back to the full screen. Returns the
    path actually written, which on WSL differs from ``out``."""
    if not is_running():
        raise RuntimeError(f"{APP_NAME} is not running. Call activate_shapr3d first.")
    if PLATFORM == "darwin":
        try:
            bounds = _osascript(
                f'tell application "System Events" to tell process "{APP_NAME}" '
                "to get {position, size} of front window"
            )
            x, y, w, h = [int(v.strip()) for v in bounds.split(",")]
            region = ["-R", f"{x},{y},{w},{h}"]
        except Exception:
            region = []
        _run(["screencapture", "-x", *region, str(out)], check=True)
        return out
    if PLATFORM in ("windows", "wsl"):
        # PowerShell writes to a Windows path; on WSL that is staged and then
        # mapped back so the caller always receives a path it can open.
        if PLATFORM == "wsl":
            staging = Path("/mnt/c/Users/Public/shapr3d-mcp")
            staging.mkdir(parents=True, exist_ok=True)
            target = staging / out.name
            host_out = _run(["wslpath", "-w", str(target)]).stdout.strip()
        else:
            target = out
            host_out = str(out)
        escaped = host_out.replace("'", "''")
        scope = _ps(f"$OUTFILE = '{escaped}'\n{_PS_SCREENSHOT}").splitlines()[-1].strip()
        if not target.exists():
            raise RuntimeError(f"Screenshot was not written to {host_out}")
        if scope == "FULLSCREEN":
            _log.warning(
                "%s window was not found; captured the full primary screen "
                "instead. A packaged app reports as running well before its "
                "window exists -- retry in a few seconds.", APP_NAME,
            )
        elif scope == "OCCLUDED":
            _log.warning(
                "%s did not come to the foreground, so the capture may show "
                "whatever window is on top of it rather than the app. Windows "
                "refuses SetForegroundWindow from a background process; click "
                "%s once and retry.", APP_NAME, APP_NAME,
            )
        return target
    raise BridgeUnsupported(
        f"Screenshots are not supported on platform '{PLATFORM}'."
    )


def status_note() -> str:
    base = (
        f"{APP_NAME} has no public API. Control is via STEP file exchange. "
        "For in-app actions (history, sketching, export), the user acts in "
        "the app or you use OS-level automation."
    )
    if PLATFORM == "wsl":
        return (
            base + " Running under WSL: app calls cross the Windows interop "
            "boundary and files outside /mnt are staged to "
            "C:\\Users\\Public\\shapr3d-mcp so the Windows app can read them."
        )
    if PLATFORM == "linux":
        return (
            base + " Running on plain Linux, where Shapr3D does not exist: "
            "the modeling, inspection and conversion tools work normally and "
            "only the app-bridge tools are unavailable."
        )
    return base
