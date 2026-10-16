"""Shapr3D MCP server.

Shapr3D has no public API, so this server pairs a real CAD kernel
(OpenCascade via build123d) with file/app bridges:

- Model anything as true B-rep solids and export STEP, which Shapr3D
  imports as fully editable bodies.
- Inspect and modify STEP/STL/BREP files exported from Shapr3D.
- Open files in Shapr3D and capture its window for visual feedback.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

from shapr3d_mcp import kernel, macos

# build123d logs verbosely at INFO; anything reaching stdout would corrupt
# the MCP stdio transport, so clamp it before the kernel is imported.
logging.getLogger("build123d").setLevel(logging.ERROR)
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

mcp = FastMCP(
    "shapr3d",
    instructions=(
        "Controls a CAD modeling workflow around Shapr3D. Shapr3D has no "
        "scripting API, so models are built with build123d (OpenCascade) and "
        "exchanged as STEP files, which Shapr3D opens as editable solid bodies. "
        "Typical flow: create_model / run_model_file -> open_in_shapr3d -> user edits in app -> "
        "user exports STEP -> diff_models / modify_model. Call "
        "build123d_guide first if you are unsure of build123d syntax. "
        "On macOS, call check_permissions once before using the Shapr3D UI "
        "tools (read_shapr3d_ui, click_shapr3d_text, shapr3d_menu, "
        "export_from_shapr3d...); they briefly take focus, so batch them. "
        "After each meaningful model change, call quick_look and show the "
        "PNG to the user - they only see images you send them. Modeling, "
        "checks and renders run in the CAD kernel and never need the "
        "Shapr3D window."
    ),
)

WORKDIR = Path(
    os.environ.get(
        "SHAPR3D_MCP_WORKDIR",
        Path(__file__).resolve().parents[2] / "models",
    )
)
WORKDIR.mkdir(parents=True, exist_ok=True)

APP_NAME = "Shapr3D"
IMPORT_FORMATS = {".step", ".stp", ".stl", ".brep", ".iges", ".igs"}

IS_WINDOWS = sys.platform == "win32"
# On Windows, Shapr3D is a Microsoft Store (MSIX) app: no install folder to
# probe and no command-line file arguments, only package activation.
WIN_EXE = "Shapr3D.exe"
WIN_PACKAGE_PREFIX = "Shapr3D.Shapr3D_"
WIN_PACKAGE_FAMILY_DEFAULT = "Shapr3D.Shapr3D_dvv5p1vgwv6mp"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "model"
    return name


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = WORKDIR / p
    return p


def _quiet_occt() -> None:
    # OCCT's readers print transfer chatter (and "*** ERR StepReaderData"
    # lines for slightly broken vendor STEP files) to fd 1, which would
    # corrupt the MCP stdio transport; clamp it like build123d's exporters do.
    from OCP.Message import Message, Message_Gravity

    for printer in Message.DefaultMessenger_s().Printers():
        printer.SetTraceLevel(Message_Gravity(Message_Gravity.Message_Fail))


def _import_iges(path: Path):
    # build123d has no import_iges; read via OCP and wrap like import_brep does.
    from build123d import Compound
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.IGESControl import IGESControl_Reader

    _quiet_occt()

    reader = IGESControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"Could not read IGES file: {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError(f"No geometry found in IGES file: {path}")
    return Compound.cast(shape)


def _import_any(path: Path):
    from build123d import Compound, Mesher, import_brep, import_step

    suffix = path.suffix.lower()
    if suffix in {".step", ".stp"}:
        _quiet_occt()
        return import_step(str(path))
    if suffix == ".stl":
        # import_stl returns one triangulation-only Face with no surface:
        # no solids, zero volume, and STEP/BREP export of it is empty.
        # Mesher sews the triangles into a proper Solid instead.
        shapes = Mesher().read(str(path))
        if not shapes:
            raise ValueError(f"No shapes found in STL file: {path}")
        return shapes[0] if len(shapes) == 1 else Compound(children=shapes)
    if suffix == ".brep":
        return import_brep(str(path))
    if suffix in {".iges", ".igs"}:
        return _import_iges(path)
    if suffix == ".3mf":
        shapes = Mesher().read(str(path))
        if not shapes:
            raise ValueError(f"No shapes found in 3MF file: {path}")
        return shapes[0] if len(shapes) == 1 else Compound(children=shapes)
    raise ValueError(
        f"Unsupported import format '{suffix}'. Supported: STEP, IGES, STL, "
        "BREP, 3MF. From Shapr3D, export as STEP (preferred) or STL."
    )


def _shape_stats(shape) -> dict:
    from build123d import Shape

    stats: dict = {}
    try:
        bb = shape.bounding_box()
        stats["bounding_box_mm"] = {
            "size": [round(v, 3) for v in (bb.size.X, bb.size.Y, bb.size.Z)],
            "min": [round(v, 3) for v in (bb.min.X, bb.min.Y, bb.min.Z)],
            "max": [round(v, 3) for v in (bb.max.X, bb.max.Y, bb.max.Z)],
        }
    except Exception:
        pass
    try:
        stats["volume_mm3"] = round(shape.volume, 3)
    except Exception:
        pass
    try:
        solids = shape.solids() if isinstance(shape, Shape) else []
        stats["solids"] = len(solids)
        stats["faces"] = len(shape.faces())
        stats["edges"] = len(shape.edges())
    except Exception:
        pass
    return stats


CACHE_DIR = WORKDIR / ".cache"


def _script_env(ctx: dict, base_dir: Path) -> dict:
    """Globals for model scripts: build123d, math, and the helpers
    cut / fuse / load_cad / include / export_part (see build123d_guide)."""
    import build123d as b3d

    env: dict = {"__builtins__": __builtins__}
    env.update({k: getattr(b3d, k) for k in dir(b3d) if not k.startswith("_")})
    env["math"] = __import__("math")
    env["cut"] = kernel.make_cut(ctx["warnings"])
    env["fuse"] = kernel.make_fuse(ctx["warnings"])

    def resolve_here(path: str) -> Path:
        p = Path(path).expanduser()
        if p.is_absolute():
            return p
        return (base_dir / p) if (base_dir / p).exists() else WORKDIR / p

    def load_cad(path: str, cache: bool = True):
        """Import a CAD file (STEP/IGES cached as BREP for fast reloads)."""
        src = resolve_here(path)
        if not src.exists():
            raise FileNotFoundError(f"No such file: {src}")
        return kernel.load_cad(src, _import_any, CACHE_DIR, cache)

    def include(path: str) -> None:
        """Run another script file in this same namespace."""
        src = resolve_here(path)
        exec(compile(src.read_text(), str(src), "exec"), env)

    def export_part(shape, name: str, formats: list[str] | None = None) -> list[str]:
        """Export an extra file (e.g. one print part) next to the result."""
        files = _export(shape, WORKDIR / _safe_name(name), formats or ["step"])
        ctx["files"].extend(files)
        return files

    env.update(load_cad=load_cad, include=include, export_part=export_part)
    return env


def _run_script(script: str, extra_globals: dict | None = None,
                ctx: dict | None = None, filename: str = "<model_script>",
                base_dir: Path | None = None):
    """Execute a build123d script and return the object bound to `result`.
    `ctx` collects boolean warnings and files written by export_part."""
    import build123d as b3d

    ctx = ctx if ctx is not None else {"warnings": [], "files": []}
    env = _script_env(ctx, base_dir or WORKDIR)
    if extra_globals:
        env.update(extra_globals)

    # stdout is the MCP stdio transport: script print()s go to stderr, and
    # OCCT readers a script calls itself (import_step) are clamped too.
    _quiet_occt()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(script, filename, "exec"), env)
    except kernel.BooleanError as err:
        raise RuntimeError(f"Boolean check failed: {err}") from None
    except Exception:
        raise RuntimeError(
            "Script raised an exception:\n" + traceback.format_exc(limit=4)
        )

    result = env.get("result")
    if result is None:
        raise RuntimeError(
            "Script must assign the final shape to a variable named `result` "
            "(a Part, Solid, Compound, or a builder like BuildPart)."
        )
    # Unwrap any builder (BuildPart -> .part, BuildSketch -> .sketch, ...)
    if isinstance(result, b3d.BuildSketch):
        # _obj is the plane-local sketch; .sketch carries the workplane.
        result = result.sketch
    elif isinstance(result, b3d.Builder):
        result = result._obj
    if getattr(result, "wrapped", None) is None:
        raise RuntimeError(
            f"`result` is {type(result).__name__}, not an exportable shape. "
            "Assign a Part, Solid, Compound, Sketch, or a builder that "
            "produced geometry."
        )
    return result


def _has_mesh_only_faces(shape) -> bool:
    from OCP.BRep import BRep_Tool

    return any(BRep_Tool.Surface_s(f.wrapped) is None for f in shape.faces())


def _output_base(src: Path, output_name: str | None, default: str,
                 formats: list[str], keep_existing: bool = False) -> Path:
    """Pick the output base path, never one that would overwrite `src`.

    With keep_existing, a defaulted name also steers clear of any file that
    already exists (e.g. converting x.stl must not clobber a sibling x.step).
    """
    def targets(b: Path) -> list[Path]:
        return [b.parent / (b.name + "." + f.lower().lstrip(".")) for f in formats]

    if output_name:
        base = WORKDIR / _safe_name(output_name)
        if src.resolve() in {t.resolve() for t in targets(base)}:
            raise ValueError(
                f"output_name '{output_name}' would overwrite the input file "
                f"{src.name}; choose a different name."
            )
        return base

    def taken(b: Path) -> bool:
        return any(
            t.resolve() == src.resolve() or (keep_existing and t.exists())
            for t in targets(b)
        )

    base = WORKDIR / _safe_name(default)
    if taken(base):
        base = WORKDIR / _safe_name(default + "_converted")
        n = 2
        while taken(base):
            base = WORKDIR / _safe_name(f"{default}_converted{n}")
            n += 1
    return base


def _export(shape, base: Path, formats: list[str]) -> list[str]:
    from build123d import Mesher, export_brep, export_gltf, export_step, export_stl

    written: list[str] = []
    for fmt in formats:
        fmt = fmt.lower().lstrip(".")
        if fmt in {"step", "brep"} and _has_mesh_only_faces(shape):
            raise RuntimeError(
                f"Cannot export {fmt}: the shape contains mesh-only faces "
                "(triangulation without surface geometry), which would write "
                "a file with no geometry."
            )
        # Not with_suffix(): names like "bracket v1.2" must keep their dots.
        out = base.parent / (base.name + "." + fmt)
        # build123d exporters return False instead of raising on failure.
        ok = True
        if fmt == "step":
            ok = export_step(shape, str(out))
        elif fmt == "stl":
            ok = export_stl(shape, str(out))
        elif fmt == "brep":
            ok = export_brep(shape, str(out))
        elif fmt == "gltf":
            ok = export_gltf(shape, str(out))
        elif fmt == "glb":
            ok = export_gltf(shape, str(out), binary=True)
        elif fmt == "3mf":
            m = Mesher()
            m.add_shape(shape)
            m.write(str(out))
        else:
            raise ValueError(f"Unsupported export format: {fmt}")
        if not ok or not out.exists():
            raise RuntimeError(
                f"Export to {out.name} failed - the shape may have no "
                f"exportable geometry for the {fmt} format."
            )
        written.append(str(out))
        if fmt == "gltf":
            # Non-binary glTF writes a .bin buffer sidecar the file refers to.
            sidecar = out.parent / (out.stem + ".bin")
            if sidecar.exists():
                written.append(str(sidecar))
    return written


def _load_params(params_file: str) -> dict:
    """Load a YAML or JSON parameter file into a plain dict."""
    import json

    src = _resolve(params_file)
    if not src.exists():
        raise FileNotFoundError(f"No such params file: {src}")
    text = src.read_text(encoding="utf-8")
    if src.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"Params file must contain a mapping at the top level, "
            f"got {type(data).__name__}."
        )
    return data


def _collect_solids(paths: list[Path]) -> list[dict]:
    """Import files and flatten to a list of {ref, solid} entries."""
    entries: list[dict] = []
    for path in paths:
        entries.extend(kernel.labelled_solids(_import_any(path), path.name))
    return entries


def _osascript(script: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout.strip()


# Windows helpers. Child processes get stdin=DEVNULL: inheriting the MCP
# stdin pipe on Windows can hang the child while the server is reading it.

def _win_package_family() -> str | None:
    """Package family name of the installed Shapr3D Store app, or None."""
    import winreg

    key = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
           r"\CurrentVersion\AppModel\Repository\Packages")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            i = 0
            while True:
                try:
                    full = winreg.EnumKey(k, i)
                except OSError:
                    return None
                # Full name: Name_Version_Arch__PublisherId
                if full.startswith(WIN_PACKAGE_PREFIX) and "__" in full:
                    return WIN_PACKAGE_PREFIX + full.rsplit("__", 1)[1]
                i += 1
    except OSError:
        return None


def _win_app_pids() -> set[int]:
    proc = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {WIN_EXE}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
    )
    pids = set()
    for line in proc.stdout.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) > 1 and cols[0].lower() == WIN_EXE.lower():
            pids.add(int(cols[1]))
    return pids


def _win_find_window() -> int | None:
    """Top-level window handle of Shapr3D, or None if it has no visible one.

    Store apps draw into a CoreWindow owned by Shapr3D.exe, hosted inside an
    ApplicationFrameWindow that belongs to ApplicationFrameHost.exe, so the
    frame is matched by its children's owning process.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pids = _win_app_pids()
    if not pids:
        return None
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def owned(hwnd) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value in pids

    def hosts_app(frame) -> bool:
        hit: list[int] = []

        def on_child(hwnd, _):
            if owned(hwnd):
                hit.append(hwnd)
                return False
            return True

        user32.EnumChildWindows(frame, enum_proc(on_child), 0)
        return bool(hit)

    match: list[int] = []

    def on_top(hwnd, _):
        if user32.IsWindowVisible(hwnd) and (owned(hwnd) or hosts_app(hwnd)):
            match.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_proc(on_top), 0)
    return match[0] if match else None


def _win_capture_window(hwnd: int, out: Path) -> None:
    """Save the window's own pixels to `out`, even if other windows cover it.

    PrintWindow with PW_RENDERFULLCONTENT asks DWM for the window's content
    (needed for GPU-rendered Store apps), then the invisible resize border is
    cropped off using the DWM frame bounds.
    """
    import ctypes
    from ctypes import wintypes

    from PIL import Image as PILImage  # ships with matplotlib

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetWindowDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]

    # Physical pixels; otherwise scaled displays report virtualized sizes.
    PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    old_ctx = user32.SetThreadDpiAwarenessContext(PER_MONITOR_AWARE_V2)
    hwnd = wintypes.HWND(hwnd)
    window_dc = mem_dc = bitmap = None
    try:
        win, frame = wintypes.RECT(), wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(win))
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(frame), ctypes.sizeof(frame),
        ) != 0:
            frame = win
        w, h = win.right - win.left, win.bottom - win.top

        window_dc = user32.GetWindowDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(wintypes.HDC(window_dc))
        bitmap = gdi32.CreateCompatibleBitmap(wintypes.HDC(window_dc), w, h)
        gdi32.SelectObject(wintypes.HDC(mem_dc), wintypes.HGDIOBJ(bitmap))
        PW_RENDERFULLCONTENT = 2
        if not user32.PrintWindow(hwnd, wintypes.HDC(mem_dc), PW_RENDERFULLCONTENT):
            raise RuntimeError("PrintWindow failed to capture the Shapr3D window.")

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        # Negative height = top-down rows; 32-bit BGRX, no palette.
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(wintypes.HDC(mem_dc), wintypes.HBITMAP(bitmap),
                               0, h, buf, ctypes.byref(header), 0):
            raise RuntimeError("GetDIBits failed to read the captured window.")
        img = PILImage.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        img.crop((
            frame.left - win.left, frame.top - win.top,
            frame.right - win.left, frame.bottom - win.top,
        )).save(out)
    finally:
        if bitmap:
            gdi32.DeleteObject(wintypes.HGDIOBJ(bitmap))
        if mem_dc:
            gdi32.DeleteDC(wintypes.HDC(mem_dc))
        if window_dc:
            user32.ReleaseDC(hwnd, wintypes.HDC(window_dc))
        user32.SetThreadDpiAwarenessContext(old_ctx)


def _win_set_clipboard(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    if not user32.OpenClipboard(None):
        raise RuntimeError("Could not open the clipboard.")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        ctypes.memmove(kernel32.GlobalLock(handle), data, size)
        kernel32.GlobalUnlock(handle)
        # On success the clipboard owns the memory; don't free it.
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise RuntimeError("Could not set clipboard data.")
    finally:
        user32.CloseClipboard()


def _app_running() -> bool:
    if IS_WINDOWS:
        return bool(_win_app_pids())
    return subprocess.run(
        ["pgrep", "-x", APP_NAME], capture_output=True, stdin=subprocess.DEVNULL
    ).returncode == 0


def _activate_app() -> None:
    if IS_WINDOWS:
        family = _win_package_family() or WIN_PACKAGE_FAMILY_DEFAULT
        # Launches the Store app, or brings it forward if already running.
        os.startfile(f"shell:AppsFolder\\{family}!App")
    else:
        subprocess.run(
            ["open", "-a", APP_NAME], check=True, timeout=15,
            stdin=subprocess.DEVNULL,
        )


def _shapr3d_history_mentions(needle: str) -> bool:
    """Check Shapr3D's own accessibility tree for a History entry containing
    `needle`. This is real verification, not a timing guess: Shapr3D exposes
    its History panel text (e.g. 'Import "foo.step"') to the accessibility
    API, confirmed by inspecting the tree directly after a real import. An
    AppleScript string literal can't safely embed arbitrary text (quotes,
    backslashes), so the needle travels via an environment variable and the
    script reads it with `system attribute`, which takes any string as-is."""
    script = '''
    tell application "System Events"
        tell process "Shapr3D"
            set needle to system attribute "SHAPR3D_MCP_NEEDLE"
            set allElems to entire contents of front window
            repeat with e in allElems
                try
                    set v to value of e
                    if v is not missing value then
                        if (v as text) contains needle then
                            return "FOUND"
                        end if
                    end if
                end try
            end repeat
            return "NOTFOUND"
        end tell
    end tell
    '''
    env = dict(os.environ, SHAPR3D_MCP_NEEDLE=needle)
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
        timeout=15, env=env,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "FOUND"


# ---------------------------------------------------------------------------
# modeling tools
# ---------------------------------------------------------------------------

@mcp.tool()
def create_model(name: str, script: str, formats: list[str] | None = None,
                 params_file: str | None = None) -> dict:
    """Create a 3D model from a build123d Python script and export it.

    The script runs with `from build123d import *` already in scope and MUST
    assign the final shape to a variable named `result`. Dimensions are in mm.

    Example script (rounded block with a through-hole):
        with BuildPart() as p:
            Box(80, 40, 20)
            fillet(p.edges().filter_by(Axis.Z), radius=5)
            Cylinder(8, 20, mode=Mode.SUBTRACT)
        result = p.part

    Args:
        name: Base file name for the exported model (no extension).
        script: build123d Python code that assigns `result`.
        formats: Export formats, default ["step"]. Options: step, stl, 3mf,
            gltf, glb, brep. STEP is what Shapr3D imports as editable solid
            bodies.
        params_file: Optional YAML/JSON file whose top-level mapping is
            exposed to the script as the variable `params`. Use this as the
            single source of truth for shared dimensions instead of
            hardcoding them, e.g. `wall = params["enclosure"]["wall"]`.
    """
    extra = {"params": _load_params(params_file)} if params_file else None
    ctx = {"warnings": [], "files": []}
    shape = _run_script(script, extra, ctx)
    base = WORKDIR / _safe_name(name)
    files = _export(shape, base, formats or ["step"])
    return _build_report(shape, ctx["files"] + files, ctx["warnings"])


def _build_report(shape, files: list[str], warnings: list[str]) -> dict:
    out = {"files": files, "stats": _shape_stats(shape)}
    parts = kernel.part_inventory(shape)
    if parts:
        out["parts"] = parts
    if warnings:
        out["boolean_warnings"] = warnings
    out["next_step"] = "Use open_in_shapr3d to load the STEP file into Shapr3D."
    return out


@mcp.tool()
def run_model_file(script_path: str, name: str, formats: list[str] | None = None,
                   params_file: str | None = None) -> dict:
    """Build a model from a script FILE (version it like code) instead of an
    inline script. Relative paths resolve against the models workspace.

    Besides build123d, scripts get these helpers:
        load_cad(path)          import STEP/IGES/STL/BREP/3MF (vendor STEP is
                                cached as BREP, so re-runs are fast)
        include(path)           run another script in the same namespace
                                (e.g. a part script inside an assembly script)
        export_part(shape, name, formats)  write extra files (print parts)
        cut(a, *tools) / fuse(a, *tools)   booleans checked against the
                                actual overlap - raise instead of silently
                                doing nothing
        params                  the params_file mapping, if given
    Give assembly children a `label` (and optionally `color`) - the report
    lists them as parts, and detect_clash names clashes after them.
    """
    src = _resolve(script_path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    extra = {"__file__": str(src)}
    if params_file:
        extra["params"] = _load_params(params_file)
    ctx = {"warnings": [], "files": []}
    shape = _run_script(src.read_text(), extra, ctx, filename=str(src),
                        base_dir=src.parent)
    files = _export(shape, WORKDIR / _safe_name(name), formats or ["step"])
    return _build_report(shape, ctx["files"] + files, ctx["warnings"])


@mcp.tool()
def modify_model(input_path: str, script: str, output_name: str | None = None,
                 formats: list[str] | None = None,
                 params_file: str | None = None) -> dict:
    """Modify an existing CAD file (e.g. one exported from Shapr3D as STEP).

    The imported geometry is available in the script as the variable
    `imported` (a build123d Shape/Compound). The script must assign the
    modified shape to `result`.

    Example script (add a hole through an imported part):
        result = imported - Pos(0, 0, 0) * Cylinder(5, 200)

    Args:
        input_path: Path to a STEP/IGES/STL/BREP/3MF file. Relative paths
            resolve against the models workspace.
        script: build123d code using `imported`, assigning `result`.
        output_name: Base name for output files. Defaults to "<input>_modified".
        formats: Export formats, default ["step"].
        params_file: Optional YAML/JSON file exposed to the script as the
            variable `params` (see create_model).
    """
    src = _resolve(input_path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    imported = _import_any(src)
    extra: dict = {"imported": imported}
    if params_file:
        extra["params"] = _load_params(params_file)
    ctx = {"warnings": [], "files": []}
    shape = _run_script(script, extra, ctx)
    base = _output_base(src, output_name, src.stem + "_modified",
                        formats or ["step"])
    files = _export(shape, base, formats or ["step"])
    out = {"files": ctx["files"] + files, "stats": _shape_stats(shape)}
    if ctx["warnings"]:
        out["boolean_warnings"] = ctx["warnings"]
    return out


@mcp.tool()
def inspect_model(path: str) -> dict:
    """Inspect a CAD file (STEP/IGES/STL/BREP/3MF): solids, bounding box,
    volume, face/edge counts. Use on files exported from Shapr3D to
    understand them before modifying. Note: IGES transfers as surface
    geometry (faces/shells), so solids/volume read 0 for IGES files."""
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    shape = _import_any(src)
    info: dict = {"file": str(src), "size_bytes": src.stat().st_size}
    info.update(_shape_stats(shape))
    try:
        per_solid = []
        for i, s in enumerate(shape.solids()):
            bb = s.bounding_box()
            per_solid.append({
                "index": i,
                "label": getattr(s, "label", "") or None,
                "volume_mm3": round(s.volume, 3),
                "size_mm": [round(v, 3) for v in (bb.size.X, bb.size.Y, bb.size.Z)],
            })
        info["solids_detail"] = per_solid
    except Exception:
        pass
    parts = kernel.part_inventory(shape)
    if parts:
        info["parts"] = parts
        info.pop("solids_detail", None)  # parts is the readable view
    return info


@mcp.tool()
def detect_clash(paths: list[str], min_overlap_mm3: float = 0.001,
                 expected: list[str] | None = None,
                 near_miss_mm: float = 0.0) -> dict:
    """Check solids for interference (clash) by pairwise boolean intersection.

    Pass one file (all solids in it are checked against each other — e.g. an
    assembly compound) or several files (solids are pooled across them).
    Assembly children with labels are reported by name ("Screws#2").
    Reference geometry like keep-out volumes can be modeled as solids and
    clash-checked against real parts to enforce interface contracts.

    Args:
        paths: One or more STEP/IGES/STL/BREP/3MF files. Relative paths
            resolve against the models workspace.
        min_overlap_mm3: Overlap volume below this is ignored (numeric noise
            from coincident faces). Default 0.001 mm^3.
        expected: Intended overlaps, reported separately and not counted
            against `clear` - e.g. threads modelled at nominal size in a
            pilot hole or hoses pushed into fittings. Each entry is
            "A|B" (pair whose refs contain A and B) or "A" (any pair
            involving A); case-insensitive substrings.
        near_miss_mm: Also report non-overlapping pairs closer than this
            (min distance), e.g. 1.0 to find tight clearances.

    Returns:
        clear: True if no unexpected pair overlaps; clashes / expected
        overlaps with volume and bounding box; near_misses with gap_mm.
    """
    resolved = []
    for p in paths:
        src = _resolve(p)
        if not src.exists():
            raise FileNotFoundError(f"No such file: {src}")
        resolved.append(src)
    entries = _collect_solids(resolved)
    if len(entries) < 2:
        raise ValueError(
            f"Need at least 2 solids to clash-check, found {len(entries)}. "
            "IGES files transfer as surfaces (no solids); convert to STEP."
        )
    expected = expected or []
    clashes, intended, near, check_errors = [], [], [], []
    pairs = 0
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            if a["part"] == b["part"] and len(paths) == 1 and a["part"] != resolved[0].name:
                # solids of one assembly part (e.g. a vendor sub-assembly)
                # are not clash-checked against each other
                continue
            pairs += 1
            gap_bb = kernel.bbox_gap(a["solid"], b["solid"])
            if gap_bb > 0:
                # Cheap reject: bounding boxes that don't intersect can't clash.
                if near_miss_mm and gap_bb < near_miss_mm:
                    gap = a["solid"].distance_to(b["solid"])
                    if gap < near_miss_mm:
                        near.append({"a": a["ref"], "b": b["ref"], "gap_mm": round(gap, 3)})
                continue
            # A failed boolean must NOT read as "no clash" - record it so the
            # caller knows the check was incomplete.
            try:
                overlap = a["solid"].intersect(b["solid"])
                vol = kernel._vol(overlap)
            except Exception as exc:
                check_errors.append(
                    {"a": a["ref"], "b": b["ref"], "error": str(exc)[:200]}
                )
                continue
            if vol > min_overlap_mm3:
                entry = {
                    "a": a["ref"],
                    "b": b["ref"],
                    "overlap_mm3": round(vol, 4),
                }
                try:
                    items = overlap if isinstance(overlap, list) else [overlap]
                    from build123d import Compound
                    obb = Compound(children=[x for x in items]).bounding_box()
                    entry["overlap_bbox_min"] = [
                        round(v, 3) for v in (obb.min.X, obb.min.Y, obb.min.Z)
                    ]
                    entry["overlap_bbox_max"] = [
                        round(v, 3) for v in (obb.max.X, obb.max.Y, obb.max.Z)
                    ]
                except Exception:
                    pass  # location is best-effort; the clash itself stands
                if any(kernel.pair_matches(pt, a["ref"], b["ref"]) for pt in expected):
                    intended.append(entry)
                else:
                    clashes.append(entry)
            elif near_miss_mm:
                # boxes overlap but the solids may still be apart
                gap = a["solid"].distance_to(b["solid"])
                if gap < near_miss_mm:
                    near.append({"a": a["ref"], "b": b["ref"], "gap_mm": round(gap, 3)})
    out = {
        "clear": not clashes and not check_errors,
        "solids": len(entries),
        "pairs_checked": pairs,
        "clashes": clashes,
        "check_errors": check_errors,
    }
    if expected:
        out["expected_overlaps"] = intended
    if near_miss_mm:
        out["near_misses"] = sorted(near, key=lambda n: n["gap_mm"])
    return out


@mcp.tool()
def query_features(path: str, part: str | None = None,
                   kinds: list[str] | None = None,
                   region_min: list[float] | None = None,
                   region_max: list[float] | None = None,
                   min_area_mm2: float = 1.0, max_results: int = 80) -> dict:
    """List geometric features of a model: cylinders (radius, axis, length,
    hole vs boss) and planes (normal, centre, area). Use it to read real
    dimensions off vendor CAD or a Shapr3D export - thread shoulders, hole
    sizes, pocket floors - before designing against them.

    Args:
        part: Only solids whose ref contains this (assembly child label,
            e.g. "VGC10" or "VGC10#3").
        kinds: Subset of ["cylinder", "plane"]; default both.
        region_min / region_max: [x, y, z] box; only faces touching it.
        min_area_mm2: Skip planes smaller than this (fillet slivers).
    """
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    kinds_set = set(kinds or ["cylinder", "plane"])
    bad = kinds_set - {"cylinder", "plane"}
    if bad:
        raise ValueError(f"Unknown kinds {sorted(bad)}; use cylinder / plane")
    entries = kernel.labelled_solids(_import_any(src), src.name)
    if part:
        entries = [e for e in entries if kernel.norm(part) in kernel.norm(e["ref"])]
        if not entries:
            raise ValueError(f"No solids match part '{part}'")
    region = None
    if region_min or region_max:
        if not (region_min and region_max):
            raise ValueError("Give both region_min and region_max")
        region = (region_min, region_max)
    feats = kernel.query_features(entries, kinds_set, region, min_area_mm2, max_results)
    return {"solids_searched": len(entries), "features": feats,
            "truncated": len(feats) >= max_results}


def _section_png(entries: list[dict], bbox, plane: str, offset: float, out: Path,
                 width: int, height: int, zoom_min=None, zoom_max=None) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath

    pl = kernel.section_plane(plane, offset)
    size = 4 * max(bbox.size.X, bbox.size.Y, bbox.size.Z, 1.0)
    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)
    palette = plt.get_cmap("tab10")
    parts, colours, areas = [], {}, {}
    for e in entries:
        faces = kernel.section_faces(e["solid"], pl, size)
        if not faces:
            continue
        if e["part"] not in colours:
            colours[e["part"]] = palette(len(colours) % 10)
            parts.append(e["part"])
        for f in faces:
            areas[e["part"]] = areas.get(e["part"], 0.0) + f.area
            verts, codes = [], []
            for ring in kernel.face_rings(f, pl):
                verts += ring + [ring[0]]
                codes += [MPath.MOVETO] + [MPath.LINETO] * (len(ring) - 1) + [MPath.CLOSEPOLY]
            ax.add_patch(PathPatch(MPath(verts, codes), facecolor=colours[e["part"]],
                                   edgecolor="black", linewidth=0.4, alpha=0.85))
    if not parts:
        plt.close(fig)
        raise ValueError(f"Plane {plane} at {offset} mm does not cut the model")
    ax.autoscale_view()
    if zoom_min and zoom_max:
        ax.set_xlim(zoom_min[0], zoom_max[0])
        ax.set_ylim(zoom_min[1], zoom_max[1])
    ax.set_aspect("equal")
    ax.grid(True, linewidth=0.3)
    axes = {"XY": ("X", "Y"), "XZ": ("X", "Z"), "YZ": ("Y", "Z")}[plane.upper()]
    ax.set_xlabel(f"{axes[0]} (mm)")
    ax.set_ylabel(f"{axes[1]} (mm)")
    normal = {"XY": "Z", "XZ": "Y", "YZ": "X"}[plane.upper()]
    ax.set_title(f"Section {plane.upper()} at {normal} = {offset:g} mm", fontsize=10)
    handles = [plt.Rectangle((0, 0), 1, 1, color=colours[p]) for p in parts]
    ax.legend(handles, [f"{p} ({areas[p]:.0f} mm²)" for p in parts], fontsize=7,
              loc="upper left", bbox_to_anchor=(1.01, 1))
    fig.savefig(out, bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    return out


def _select(entries: list[dict], include: list[str] | None, exclude: list[str] | None) -> list[dict]:
    if include:
        entries = [e for e in entries if any(kernel.norm(p) in kernel.norm(e["ref"]) for p in include)]
    if exclude:
        entries = [e for e in entries if not any(kernel.norm(p) in kernel.norm(e["ref"]) for p in exclude)]
    if not entries:
        raise ValueError("No solids left after include/exclude filters")
    return entries


@mcp.tool()
def section_view(path: str, plane: str = "XZ", offset: float = 0.0,
                 width: int = 900, height: int = 900,
                 zoom_min: list[float] | None = None,
                 zoom_max: list[float] | None = None) -> Image:
    """Cut the model with a plane and draw the cross-section in 2-D (parts
    coloured separately, mm grid). Shows wall thickness, internal channels
    and whether cuts really happened - things a shaded render hides.

    Args:
        plane: "XY", "XZ" or "YZ".
        offset: Plane position along its normal (mm), e.g. y=0 for XZ.
        zoom_min / zoom_max: [u, v] corners in the section's own axes
            (e.g. [x, z] for XZ) to zoom into a detail.
    """
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    shape = _import_any(src)
    tag = "_zoom" if zoom_min and zoom_max else ""
    out = WORKDIR / f"{_safe_name(src.stem)}_section_{plane.upper()}_{offset:g}{tag}.png"
    _section_png(kernel.labelled_solids(shape, src.name), shape.bounding_box(), plane,
                 offset, out, width, height, zoom_min, zoom_max)
    return Image(path=str(out))


@mcp.tool()
def quick_look(path: str, include: list[str] | None = None,
               exclude: list[str] | None = None, plane: str = "XZ",
               offset: float = 0.0) -> dict:
    """One progress picture: iso + front render and a cross-section side by
    side, written to <name>_quicklook.png. Call it after every meaningful
    change and SHOW THE PNG TO THE USER (e.g. send the file) - they cannot
    see tool images, and progress pictures let them steer early.

    Args:
        include / exclude: Part-name filters (substrings of assembly labels),
            e.g. exclude=["VGC10"] to keep big vendor parts out of the render
            (the section still uses everything not excluded).
        plane / offset: Section plane, as in section_view.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    import matplotlib.pyplot as plt
    from build123d import Compound

    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    shape = _import_any(src)
    entries = _select(kernel.labelled_solids(shape, src.name), include, exclude)
    sub = Compound(children=[e["solid"] for e in entries])
    stem = _safe_name(src.stem)
    with tempfile.TemporaryDirectory() as td:
        views = Path(td) / "views.png"
        sec = Path(td) / "section.png"
        palette = plt.get_cmap("tab10")
        names = list(dict.fromkeys(e["part"] for e in entries))  # section order
        groups = [(Compound(children=[e["solid"] for e in entries if e["part"] == n]),
                   palette(i % 10)[:3]) for i, n in enumerate(names)]
        _render_png(sub, views, [("iso", 25, -60, False), ("front", 0, -90, True)],
                    1000, 900, False, parts=groups)
        _section_png(entries, sub.bounding_box(), plane, offset, sec, 800, 900)
        fig, axs = plt.subplots(1, 2, figsize=(18, 9), dpi=100,
                                gridspec_kw={"width_ratios": [1.1, 1]})
        for ax, img in zip(axs, (views, sec)):
            ax.imshow(mpimg.imread(img))
            ax.set_axis_off()
        fig.suptitle(f"{src.name} - {len(entries)} solids", fontsize=12)
        out = WORKDIR / f"{stem}_quicklook.png"
        fig.savefig(out, bbox_inches="tight", pad_inches=0.1)
        plt.close(fig)
    return {"image": str(out),
            "parts": sorted({e["part"] for e in entries}),
            "next_step": "Send this PNG to the user so they can see progress."}


@mcp.tool()
def diff_models(before: str, after: str, part: str | None = None,
                min_volume_mm3: float = 0.01) -> dict:
    """Compare two versions of a model - e.g. the STEP you sent to Shapr3D
    and the STEP the user exported after editing it there - to see what
    changed: material added/removed (regions with bounding boxes), volume
    change, and faces present in only one version (a chamfer shows up as
    new angled planes, a fillet as new cylinders/tori).

    Args:
        part: Restrict to solids whose ref contains this (assembly label).
            Both files must contain a match.
    """
    srcs = []
    for p in (before, after):
        src = _resolve(p)
        if not src.exists():
            raise FileNotFoundError(f"No such file: {src}")
        srcs.append(src)
    shapes = []
    for src in srcs:
        entries = kernel.labelled_solids(_import_any(src), src.name)
        if part:
            entries = [e for e in entries if kernel.norm(part) in kernel.norm(e["ref"])]
            if not entries:
                raise ValueError(f"No solids match part '{part}' in {src.name}")
        from build123d import Compound
        solids = [e["solid"] for e in entries]
        shapes.append(solids[0] if len(solids) == 1 else Compound(children=solids))
    report = kernel.diff_solids(shapes[0], shapes[1], min_volume_mm3)
    report["before"], report["after"] = str(srcs[0]), str(srcs[1])
    return report


@mcp.tool()
def convert_model(path: str, formats: list[str], output_name: str | None = None) -> dict:
    """Convert a CAD file to other formats. Reads STEP/IGES/STL/BREP/3MF and
    writes step, stl, 3mf, gltf, glb, brep (glTF is export-only). E.g.
    convert an STL mesh to STEP-wrapped geometry, or STEP to STL for 3D
    printing. Without output_name, outputs are named after the input but
    never overwrite an existing file (a "_converted" suffix is added)."""
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    shape = _import_any(src)
    # Defaults to the source stem, but never over the source file itself.
    # Shape is imported first so a bad file fails before anything is written.
    base = _output_base(src, output_name, src.stem, formats, keep_existing=True)
    return {"files": _export(shape, base, formats)}


VIEW_PRESETS = {
    # (elevation, azimuth) in degrees, matplotlib convention.
    "iso": (30, -60),
    "front": (0, -90),   # looking along +Y
    "back": (0, 90),
    "right": (0, 0),     # looking along -X
    "left": (0, 180),
    "top": (90, -90),    # looking down -Z
    "bottom": (-90, -90),
}


@mcp.tool()
def render_preview(path: str, width: int = 800, height: int = 600,
                   views: list[str] | None = None,
                   elevation: float | None = None,
                   azimuth: float | None = None,
                   edges: bool = False) -> Image:
    """Render a PNG preview of a CAD file (STEP/IGES/STL/BREP/3MF) without
    opening Shapr3D. Useful to visually verify a model you just created
    before sending it to the app.

    Args:
        views: One or more of iso, front, back, left, right, top, bottom,
            or ["all"] for an iso/front/top/right grid. Default ["iso"].
            Standard views (not iso) use orthographic projection.
        elevation, azimuth: Custom camera angles in degrees; override
            `views` with a single custom view.
        edges: Overlay B-rep edges as dark lines (drawn through the part,
            like a wireframe, so hidden edges also show).
    """
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    shape = _import_any(src)
    if elevation is not None or azimuth is not None:
        cams = [("custom", elevation if elevation is not None else 30,
                 azimuth if azimuth is not None else -60, False)]
    else:
        names = views or ["iso"]
        if names == ["all"]:
            names = ["iso", "front", "top", "right"]
        unknown = [v for v in names if v not in VIEW_PRESETS]
        if unknown:
            raise ValueError(
                f"Unknown view(s) {unknown}; choose from "
                f"{sorted(VIEW_PRESETS)} or 'all'."
            )
        cams = [(v, *VIEW_PRESETS[v], v != "iso") for v in names]
    out = WORKDIR / (_safe_name(src.stem) + "_preview.png")
    _render_png(shape, out, cams, width, height, edges)
    return Image(path=str(out))


DEFAULT_RGB = (0.35, 0.55, 0.85)


def _render_png(shape, out: Path, cams: list[tuple], width: int, height: int,
                edges: bool, parts: list[tuple] | None = None) -> None:
    """Render via STL -> matplotlib (no GPU/display needed). `parts` is an
    optional [(shape, rgb)] list to colour assembly parts separately."""
    import numpy as np
    from build123d import export_stl

    parts = parts or [(shape, DEFAULT_RGB)]
    # tessellate to roughly pixel size: the default 0.001 mm tolerance
    # turns vendor CAD into millions of triangles matplotlib crawls on
    diag = shape.bounding_box().diagonal
    tri_list, nrm_list, rgb_list = [], [], []
    with tempfile.TemporaryDirectory() as td:
        for k, (part, rgb) in enumerate(parts):
            stl_path = Path(td) / f"p{k}.stl"
            export_stl(part, str(stl_path), tolerance=max(diag / 3000, 0.002),
                       angular_tolerance=0.25)
            t, n = _read_binary_stl(stl_path)
            tri_list.append(t)
            nrm_list.append(n)
            rgb_list.append(np.tile(np.array(rgb, dtype=float), (len(t), 1)))
    tris, normals, base_rgb = (np.concatenate(x) for x in (tri_list, nrm_list, rgb_list))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection

    edge_lines = []
    if edges:
        all_edges = shape.edges()
        if len(all_edges) <= 5000:  # meshes (STL) have one edge per triangle side
            for e in all_edges:
                n = 2 if e.geom_type.name == "LINE" else 24
                pts = [e.position_at(i / (n - 1)) for i in range(n)]
                xyz = [(p.X, p.Y, p.Z) for p in pts]
                # Line3DCollection needs equal-length segments: use pairs.
                edge_lines.extend(zip(xyz[:-1], xyz[1:]))

    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    center, span = (lo + hi) / 2, (hi - lo).max() / 2 or 1
    size = hi - lo

    cols = 1 if len(cams) == 1 else 2
    rows = (len(cams) + cols - 1) // cols
    fig = plt.figure(figsize=(width / 100, height / 100), dpi=100)
    for i, (label, elev, azim, ortho) in enumerate(cams):
        ax = fig.add_subplot(rows, cols, i + 1, projection="3d")
        if ortho:
            ax.set_proj_type("ortho")
        # Headlight from the camera, nudged up-left so faces square to the
        # view still differ from their neighbours.
        e, a = np.radians(elev), np.radians(azim)
        cam = np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])
        light = cam + 0.35 * np.array([-np.sin(a), np.cos(a), 0.6])
        light = light / np.linalg.norm(light)
        shade = 0.3 + 0.7 * np.clip(normals @ light, 0, 1)
        colors = base_rgb * shade[:, None]
        # Stroke each triangle in its own colour: with edgecolor="none" the
        # antialiased gaps between triangles show up as light seams.
        coll = Poly3DCollection(
            tris, facecolors=colors, edgecolors=colors, linewidths=0.4
        )
        ax.add_collection3d(coll)
        if edge_lines:
            ax.add_collection3d(
                Line3DCollection(edge_lines, colors="#111111", linewidths=0.6)
            )
        # box aspect = real extents, so tall/flat parts fill the frame
        ext = np.maximum(size, 1e-3 * max(size.max(), 1))
        for setter, c, h in zip((ax.set_xlim, ax.set_ylim, ax.set_zlim), center, ext / 2):
            setter(c - h, c + h)
        ax.set_box_aspect(ext, zoom=1.0)
        ax.set_axis_off()
        ax.view_init(elev=elev, azim=azim)
        if len(cams) > 1 or label != "iso":
            ax.set_title(label, fontsize=9)
    fig.suptitle(
        f"{size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm (X x Y x Z)",
        fontsize=8, y=0.02, va="bottom",
    )
    fig.savefig(out, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


def _read_binary_stl(path: Path):
    import numpy as np
    import struct

    data = path.read_bytes()
    n = struct.unpack_from("<I", data, 80)[0]
    rec = np.frombuffer(data, dtype=np.uint8, count=n * 50, offset=84)
    rec = rec.reshape(n, 50)
    floats = rec[:, :48].copy().view("<f4").reshape(n, 12)
    normals = floats[:, 0:3]
    tris = floats[:, 3:12].reshape(n, 3, 3)
    return tris, normals


@mcp.tool()
def list_models() -> list[dict]:
    """List CAD files in the models workspace directory."""
    out = []
    exts = IMPORT_FORMATS | {".3mf", ".gltf", ".glb", ".png", ".shapr"}
    for p in sorted(WORKDIR.iterdir()):
        if p.suffix.lower() not in exts or not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue  # broken symlink or vanished file; skip, don't abort
        out.append({
            "path": str(p),
            "size_bytes": st.st_size,
            "modified": st.st_mtime,
        })
    return out


# ---------------------------------------------------------------------------
# Shapr3D app bridge (macOS / Windows)
# ---------------------------------------------------------------------------

IMPORT_DIALOG_NOTE = (
    "The app shows an Import Preferences dialog (Quality/Speed/Custom); the "
    "user clicks Import. STEP files carry their units (mm), so no unit "
    "choice is needed."
)


@mcp.tool()
def open_in_shapr3d(path: str, new_project: bool = True) -> str:
    """Open a CAD file in Shapr3D as a real editable body.

    Verified against Shapr3D 26.143.0 - two things the original approach got
    wrong, found by testing, not assumed:

    1. Plain `open -a Shapr3D file.step` does NOT reliably import geometry.
       With no project open it silently creates an empty project titled after
       the file (workspace stays empty - confirmed via the History panel).
       With a project already open it instead pops Shapr3D's own "Import to:
       Current Project / New Project" dialog, whose timing/default-button
       behavior isn't something to drive blindly.
    2. The only flow that imports deterministically is the in-app menu: File >
       Import File... (Shift-Cmd-I) from a known project state, then the
       standard macOS open panel's go-to-folder (Shift-Cmd-G) to jump straight
       to the file. So this function always starts from an explicit New
       Project (Cmd-N) rather than relying on `open -a` with a path argument,
       which sidesteps the ambiguous native dialog entirely.

    Requires the calling app to have Accessibility + Automation (System
    Events) permission in System Settings > Privacy & Security - macOS will
    prompt for this on first use if not already granted.

    Args:
        path: File to import (STEP preferred - imports as an editable body).
        new_project: Start a fresh project before importing (default True).
            Set False to import into whatever project is already frontmost.

    Raises:
        RuntimeError: if the History panel doesn't show this file as imported
            after retrying. This is checked for real (via Shapr3D's own
            accessibility tree, which exposes its History text) rather than
            assumed from timing - a silent no-op import (the exact failure
            mode this replaced) is a wrong answer, not a warning to skip.

    On Windows, Shapr3D (a Store app) only accepts .shapr files from other
    programs. For STEP and other formats this launches Shapr3D and puts the
    file's full path on the clipboard; the user then imports it in the app.
    """
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    if IS_WINDOWS:
        if src.suffix.lower() == ".shapr":
            os.startfile(str(src))  # .shapr is the one type Shapr3D registers
            return f"Opened {src.name} in Shapr3D."
        _activate_app()
        _win_set_clipboard(str(src))
        return (
            f"Shapr3D is launched/activated, but on Windows it cannot be handed "
            f"{src.suffix} files directly. The full path of {src.name} is on the "
            "clipboard: tell the user to choose Import in Shapr3D, paste the path "
            "(Ctrl+V) into the file picker's file name box and press Enter, or "
            f"drag the file onto the Shapr3D window. Path: {src}. "
            f"{IMPORT_DIALOG_NOTE}"
        )

    import time

    def _attempt() -> None:
        subprocess.run(["open", "-a", APP_NAME], check=True, timeout=15)
        time.sleep(1.0)

        path_str = str(src).replace("\\", "\\\\").replace('"', '\\"')
        new_project_step = (
            'keystroke "n" using {command down}\n            delay 0.8'
            if new_project else ""
        )
        script = f'''
        tell application "{APP_NAME}" to activate
        delay 0.4
        tell application "System Events"
            tell process "{APP_NAME}"
                {new_project_step}
                keystroke "i" using {{shift down, command down}}
                delay 0.8
                keystroke "g" using {{shift down, command down}}
                delay 0.4
                keystroke "{path_str}"
                delay 0.3
                key code 36
                delay 0.8
                key code 36
            end tell
        end tell
        '''
        _osascript(script)

    needle = f'Import "{src.name}"'
    attempts = 0
    for attempts in (1, 2):
        _attempt()
        # The import (or the failure mode this replaced - a silent empty
        # project) both finish well under a second once triggered; poll
        # briefly rather than guessing a single fixed delay.
        confirmed = False
        for _ in range(10):
            if _shapr3d_history_mentions(needle):
                confirmed = True
                break
            time.sleep(0.5)
        if confirmed:
            return (
                f"Imported {src.name} into a "
                f"{'new' if new_project else 'the current'} Shapr3D project "
                f"via File > Import File... (UI-automated - Shapr3D has no "
                f"API for this). Confirmed via the History panel"
                f"{' after a retry' if attempts == 2 else ''} - not just "
                "assumed from timing."
            )

    raise RuntimeError(
        f"Could not confirm {src.name} was imported into Shapr3D after "
        f"{attempts} attempts (checked the History panel for "
        f"'{needle}' and didn't find it). Call screenshot_shapr3d to see "
        "what actually happened - a dialog may be blocking input, or "
        "Accessibility/Automation permission may need re-granting in "
        "System Settings > Privacy & Security."
    )


@mcp.tool()
def shapr3d_status() -> dict:
    """Check whether Shapr3D is installed, running, and frontmost."""
    frontmost = None
    if IS_WINDOWS:
        import ctypes

        installed = _win_package_family() is not None
        running = _app_running()
        if running:
            hwnd = _win_find_window()
            frontmost = (
                hwnd is not None
                and ctypes.windll.user32.GetForegroundWindow() == hwnd
            )
    else:
        installed = Path(f"/Applications/{APP_NAME}.app").exists()
        running = _app_running()
        if running:
            try:
                front = _osascript(
                    'tell application "System Events" to get name of first '
                    "application process whose frontmost is true"
                )
                frontmost = front == APP_NAME
            except Exception:
                pass
    return {
        "installed": installed,
        "running": running,
        "frontmost": frontmost,
        "workspace": str(WORKDIR),
        "note": (
            "Shapr3D has no public API. Control is via STEP file exchange. "
            "For in-app actions (history, sketching, export), the user acts "
            "in the app or you use OS-level automation (computer use)."
        ),
    }


@mcp.tool()
def activate_shapr3d() -> str:
    """Launch Shapr3D (if needed) and bring it to the foreground."""
    _activate_app()
    return "Shapr3D activated."


@mcp.tool()
def screenshot_shapr3d() -> Image:
    """Capture a screenshot of the Shapr3D window to see the current state
    of the model/app. On macOS, requires Screen Recording permission for the
    host process the first time; captures the window even when covered and
    scales it to window points (pixel coords = click_shapr3d_at coords). On Windows, captures the window even when other
    windows cover it, but not when it is minimized."""
    if not _app_running():
        raise RuntimeError("Shapr3D is not running. Call activate_shapr3d first.")
    out = WORKDIR / "_shapr3d_screenshot.png"
    if IS_WINDOWS:
        import ctypes

        hwnd = _win_find_window()
        if hwnd is None or ctypes.windll.user32.IsIconic(hwnd):
            raise RuntimeError(
                "Shapr3D has no visible window (minimized?). Call "
                "activate_shapr3d first."
            )
        _win_capture_window(hwnd, out)
        return Image(path=str(out))
    try:
        # by window id: works when other windows cover Shapr3D, and is
        # scaled to window points so pixels map 1:1 to click_shapr3d_at
        macos.capture(out)
        return Image(path=str(out))
    except Exception:
        pass
    try:
        bounds = _osascript(
            f'tell application "System Events" to tell process "{APP_NAME}" to '
            "get {position, size} of front window"
        )
        x, y, w, h = [int(v.strip()) for v in bounds.split(",")]
        region = ["-R", f"{x},{y},{w},{h}"]
    except Exception:
        region = []  # fall back to full screen
    subprocess.run(
        ["screencapture", "-x", *region, str(out)], check=True, timeout=15,
        stdin=subprocess.DEVNULL,
    )
    return Image(path=str(out))


def _require_running() -> None:
    """UI automation (OCR, clicks, menus, keys, Save panel) is macOS-only."""
    if sys.platform != "darwin":
        raise RuntimeError(
            "This Shapr3D UI tool is macOS-only (it uses Accessibility, "
            "Vision OCR and System Events). Model tools work everywhere."
        )
    if not macos.is_running():
        raise RuntimeError("Shapr3D is not running. Call activate_shapr3d first.")


@mcp.tool()
def read_shapr3d_ui(contains: str | None = None) -> dict:
    """OCR the Shapr3D window: every visible text label with its centre in
    window points (x, y; same frame as screenshot_shapr3d). Use it to find
    buttons, dialog options and tool names before clicking.

    Args:
        contains: Optional case-insensitive filter on the label text.
    """
    _require_running()
    win, items = macos.read_ui(WORKDIR / "_shapr3d_ui.png")
    if contains:
        items = [i for i in items if contains.lower() in i["text"].lower()]
    return {
        "window_points": [win["w"], win["h"]],
        "labels": [{k: i[k] for k in ("text", "x", "y")} for i in items],
    }


@mcp.tool()
def click_shapr3d_text(text: str, occurrence: int = 1, wait_s: float = 0) -> dict:
    """Click a visible text label in Shapr3D (button, dialog option, tool or
    list item), located by OCR. Exact matches beat substring matches;
    `occurrence` picks the n-th match top-to-bottom. `wait_s` keeps looking
    for the label that long (e.g. for a dialog that is still opening).
    Brings Shapr3D to the front."""
    _require_running()
    deadline = time.monotonic() + wait_s
    shot = WORKDIR / "_shapr3d_ui.png"
    while True:
        _, items = macos.read_ui(shot)
        hit = macos.match_text(text, items, occurrence)
        if hit or time.monotonic() >= deadline:
            break
        time.sleep(0.8)
    if not hit:
        seen = sorted({i["text"] for i in items})[:60]
        raise RuntimeError(f"'{text}' not found on screen. Visible labels: {seen}")
    macos.activate()
    macos.click_screen(hit["screen_x"], hit["screen_y"])
    return {"clicked": hit["text"], "at_window_points": [hit["x"], hit["y"]]}


@mcp.tool()
def click_shapr3d_at(x: float, y: float) -> dict:
    """Click at window coordinates in points (the pixel coordinates of a
    screenshot_shapr3d image) - for icons without a text label. Brings
    Shapr3D to the front."""
    _require_running()
    win = macos.window()
    if not (0 <= x <= win["w"] and 0 <= y <= win["h"]):
        raise ValueError(f"({x}, {y}) is outside the {win['w']}x{win['h']} window")
    macos.activate()
    macos.click_screen(win["x"] + x, win["y"] + y)
    return {"clicked_window_points": [x, y]}


@mcp.tool()
def shapr3d_menu(path: list[str] | None = None, click: bool = True) -> dict:
    """Use Shapr3D's menu bar, e.g. ["File", "Export to", "STEP…"] or
    ["Edit", "Undo"]. Menu names must match exactly (note the "…"
    ellipsis character). With click=False, or a path ending at a menu,
    lists the items under that path instead (empty path = top menus)."""
    _require_running()
    path = path or []
    if click and path:
        try:
            macos.activate()
            macos.menu(path)
            return {"clicked": path}
        except RuntimeError as err:
            if "Can't get" not in str(err) and "Invalid index" not in str(err):
                raise
            # fall through: show what exists at the deepest valid level
    for depth in range(len(path), -1, -1):
        try:
            return {"path": path[:depth], "items": macos.menu_items(path[:depth]),
                    **({"error": f"'{path[depth]}' not found here"} if depth < len(path) else {})}
        except RuntimeError:
            continue
    raise RuntimeError("Could not read the Shapr3D menu bar.")


@mcp.tool()
def shapr3d_keys(combo: str | None = None, text: str | None = None) -> dict:
    """Send a keyboard shortcut to Shapr3D ("cmd+z", "escape", "return",
    "cmd+shift+s", "delete") and/or type text (e.g. a value into a focused
    field). Brings Shapr3D to the front."""
    _require_running()
    if not combo and text is None:
        raise ValueError("Give a combo, text, or both")
    parsed = macos.parse_keys(combo) if combo else None  # validate first
    macos.activate()
    if text is not None:
        macos.type_text(text)
    if combo:
        macos.keys(combo)
    return {"sent": combo, "typed": text, "parsed": parsed}


EXPORT_MENU = {
    "step": "STEP…", "stl": "STL…", "3mf": "3MF…", "iges": "IGES…",
    "glb": "GLB…", "obj": "OBJ…", "usdz": "USDZ…", "x_t": "Parasolid file…",
    "shapr": "Shapr3D…",
}
EXPORT_EXT = {"x_t": "x_t", "iges": "igs"}


@mcp.tool()
def export_from_shapr3d(name: str, fmt: str = "step", folder: str | None = None,
                        timeout_s: float = 15) -> dict:
    """Export the open Shapr3D project (File > Export to > <format>) and save
    it, so you can inspect / diff / modify what the user built or changed in
    the app. Shapr3D takes focus for ~3 s while the Save panel is filled in,
    then focus returns to the previously active app.

    Args:
        name: File name without extension.
        fmt: step (default), stl, 3mf, iges, glb, obj, usdz, x_t, shapr.
        folder: Target folder; default the models workspace.
    Returns the saved path (extension added if Shapr3D left it off) and,
    for CAD formats, inspect_model's summary.
    """
    _require_running()
    fmt = fmt.lower().lstrip(".")
    if fmt not in EXPORT_MENU:
        raise ValueError(f"fmt must be one of {sorted(EXPORT_MENU)}")
    dest = Path(folder).expanduser() if folder else WORKDIR
    dest.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(name)
    ext = EXPORT_EXT.get(fmt, fmt)
    before = {p for p in dest.iterdir()} if dest.exists() else set()
    previous = macos.frontmost_app()
    macos.menu(["File", "Export to", EXPORT_MENU[fmt]])
    if not macos.wait_for_save_dialog(timeout_s):
        _, items = macos.read_ui(WORKDIR / "_shapr3d_ui.png")
        raise RuntimeError(
            "No Save dialog appeared (Shapr3D may show export options first, "
            "or be in a mode that blocks export). Visible labels: "
            f"{sorted({i['text'] for i in items})[:40]}"
        )
    try:
        macos.activate()
        macos.fill_save_dialog(safe, str(dest))
    finally:
        if previous and previous != APP_NAME:
            macos.activate_app(previous)
    deadline = time.monotonic() + 30
    saved = None
    while time.monotonic() < deadline and saved is None:
        for cand in (dest / f"{safe}.{ext}", dest / safe):
            if cand.exists() and cand not in before and cand.stat().st_size > 0:
                saved = cand
                break
        else:
            time.sleep(0.5)
    if saved is None:
        raise RuntimeError(f"Export did not appear in {dest} within 30 s")
    if saved.suffix.lower() != f".{ext}":
        final = saved.with_name(f"{safe}.{ext}")
        saved.rename(final)  # Shapr3D omits the extension when typed by name
        saved = final
    out = {"saved": str(saved), "focus_returned_to": previous}
    if fmt in {"step", "stl", "3mf", "iges"}:
        info = inspect_model(str(saved))
        out["summary"] = {k: info[k] for k in ("solids", "volume_mm3", "bounding_box_mm") if k in info}
    return out


@mcp.tool()
def check_permissions(prompt: bool = True) -> dict:
    """Warm-up: check (and with prompt=True, request) every macOS permission
    the Shapr3D UI tools need - Accessibility, Screen Recording, control of
    System Events - so the user approves them once at the start instead of
    mid-task. Model tools (create/inspect/clash/render...) need none.

    Claude's own per-tool approvals are separate: one allow rule
    "mcp__shapr3d" in Claude's settings covers all tools of this server.
    """
    if sys.platform != "darwin":
        return {"ready": True, "note": "No macOS permissions to request on this platform."}
    status = macos.permissions(prompt)
    missing = [k for k in ("accessibility", "screen_recording", "automation_system_events")
               if not status.get(k)]
    status["shapr3d_running"] = macos.is_running()
    status["ready"] = not missing
    if missing:
        status["todo"] = (
            f"Grant {', '.join(missing)} to the app hosting this server "
            "(System Settings > Privacy & Security), then restart it. "
            "Screen Recording changes only apply after a restart."
        )
    status["claude_tool_approvals"] = (
        "Allow rule 'mcp__shapr3d' (Claude settings, permissions.allow) "
        "pre-approves every tool of this server."
    )
    return status


# ---------------------------------------------------------------------------
# reference
# ---------------------------------------------------------------------------

@mcp.tool()
def build123d_guide() -> str:
    """Cheat sheet for writing build123d scripts for create_model /
    modify_model. Read this before writing modeling code."""
    return GUIDE


GUIDE = """\
# build123d quick reference (units: mm)

Assign the final shape to `result`. Builder mode is recommended:

    with BuildPart() as p:
        Box(60, 40, 10)                                  # centered at origin
        with Locations((20, 0, 5)):
            Cylinder(radius=5, height=10, mode=Mode.SUBTRACT)
        fillet(p.edges().filter_by(Axis.Z), radius=3)
    result = p.part

## Primitives (BuildPart)
Box(l, w, h), Cylinder(r, h), Cone(r1, r2, h), Sphere(r), Torus(r1, r2),
Wedge(...), Hole(r, depth), CounterBoreHole(...), CounterSinkHole(...)

## Sketch -> 3D
    with BuildPart() as p:
        with BuildSketch() as sk:
            RectangleRounded(80, 50, radius=8)
            with Locations((0, 10)): Circle(12, mode=Mode.SUBTRACT)
        extrude(amount=15)
    result = p.part
Also: revolve(axis=Axis.Z), loft(), sweep(path), offset(amount=-2, openings=...)
for shelling, mirror(about=Plane.XZ).

## Sketch shapes
Rectangle, RectangleRounded, Circle, Ellipse, Polygon, RegularPolygon,
Text("hi", font_size=10), SlotOverall(width, height), Trapezoid

## Placement
with Locations((x, y, z), (x2, y2, z2)): ...      # multiple copies
with PolarLocations(radius=30, count=6): ...      # circular pattern
with GridLocations(20, 20, 4, 3): ...             # grid pattern
workplanes: BuildSketch(Plane.XZ), BuildSketch(p.faces().sort_by(Axis.Z)[-1])
Pos(x, y, z) * shape and Rot(X=90) * shape in algebra mode.

## Booleans
mode=Mode.ADD (default), Mode.SUBTRACT, Mode.INTERSECT inside builders.
Algebra mode: result = Box(10,10,10) - Cylinder(3, 20)   (+, -, &)

## Selectors
p.edges().filter_by(Axis.Z)            # edges parallel to Z
p.faces().sort_by(Axis.Z)[-1]          # topmost face
p.edges().filter_by(GeomType.CIRCLE)   # circular edges
p.edges().group_by(Axis.Z)[-1]         # edges at max Z
fillet(edges, radius=2), chamfer(edges, length=1)

## Threads/text/etc
Threads: use build123d objects or import; for Shapr3D round-trip prefer
plain geometry. Text on faces: BuildSketch on a face + Text + extrude.

## Helpers available in every script (create_model / modify_model / run_model_file)
cut(a, *tools), fuse(a, *tools)   booleans checked against the real overlap:
                                  raise if OpenCascade silently did nothing,
                                  warn (boolean_warnings) if a tool missed.
load_cad("vendor/x.step")         import STEP/IGES/STL/BREP/3MF; STEP/IGES are
                                  cached as BREP, so re-runs are fast.
include("part.py")                run another script in the same namespace.
export_part(shape, "name", ["step", "stl"])   extra output files.
Label assembly children (shape.label = "Bracket", shape.color = Color(...)):
reports list them as parts and detect_clash names clashes after them.

## Pitfalls (each cost real debugging time)
- loft()/extrude() in algebra mode return a Part wrapping a Compound;
  `part - that` can silently do nothing. Use cut()/fuse() or .solids()[0].
- Tool pieces offset by ~0.01 mm (z_top + 0.01 on one, z_top on the next)
  make booleans fail silently. Fuse pieces on an exact shared face first.
- `Box(...).fillet(...)` / `.chamfer(...)` on a primitive raises a
  TypeError; use Solid(box.wrapped).fillet(...) or the BuildPart fillet().
- Filters by position need a size/depth condition too: a part centred on
  the axis "matches" every port at (0, 0).

## Checking work without Shapr3D
query_features (holes, bosses, planes - real dimensions of vendor CAD),
section_view (2-D cut: walls, channels, whether cuts happened),
detect_clash(expected=[...threads, hoses], near_miss_mm=...),
diff_models (what the user changed after editing in Shapr3D).

## Tips for Shapr3D round-trips
- Always export STEP for editability in Shapr3D (STL is a dumb mesh).
- Keep bodies as separate solids (a Compound of solids imports as
  separate bodies in Shapr3D).
- Shapr3D works in real-world units; this kernel is mm.
"""


def _preload_native_modules() -> None:
    """Import every native-extension module the tools use, before serving.

    The stdio transport reads stdin on a worker thread with a blocking read.
    On Windows, loading some DLLs (numpy's, among others) while that read is
    pending deadlocks the process, so the lazy imports inside the tools
    would hang the first tool call forever. Loading them up front avoids it.
    """
    import ctypes  # noqa: F401

    import build123d  # noqa: F401
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot  # noqa: F401
    import numpy  # noqa: F401
    import yaml  # noqa: F401
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: F401
    from OCP.IFSelect import IFSelect_RetDone  # noqa: F401
    from OCP.IGESControl import IGESControl_Reader  # noqa: F401
    from OCP.Message import Message  # noqa: F401
    from PIL import Image  # noqa: F401


def main() -> None:
    if IS_WINDOWS:
        _preload_native_modules()
    mcp.run()


if __name__ == "__main__":
    main()
