"""Shapr3D MCP server.

Shapr3D has no public API, so this server pairs a real CAD kernel
(OpenCascade via build123d) with file/app bridges:

- Model anything as true B-rep solids and export STEP, which Shapr3D
  imports as fully editable bodies.
- Inspect and modify STEP/STL/BREP files exported from Shapr3D.
- Open files in Shapr3D and capture its window for visual feedback.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

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
        "Typical flow: create_model -> open_in_shapr3d -> user edits in app -> "
        "user exports STEP -> inspect_model / modify_model. Call "
        "build123d_guide first if you are unsure of build123d syntax."
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


def _import_iges(path: Path):
    # build123d has no import_iges; read via OCP and wrap like import_brep does.
    from build123d import Compound
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.IGESControl import IGESControl_Reader
    from OCP.Message import Message, Message_Gravity

    # OCCT's reader prints transfer chatter to fd 1, which would corrupt the
    # MCP stdio transport; clamp it like build123d's exporters do.
    for printer in Message.DefaultMessenger_s().Printers():
        printer.SetTraceLevel(Message_Gravity(Message_Gravity.Message_Fail))

    reader = IGESControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"Could not read IGES file: {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError(f"No geometry found in IGES file: {path}")
    return Compound.cast(shape)


def _import_any(path: Path):
    from build123d import Compound, Mesher, import_brep, import_step, import_stl

    suffix = path.suffix.lower()
    if suffix in {".step", ".stp"}:
        return import_step(str(path))
    if suffix == ".stl":
        return import_stl(str(path))
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


def _run_script(script: str, extra_globals: dict | None = None):
    """Execute a build123d script and return the object bound to `result`."""
    import build123d as b3d

    env: dict = {"__builtins__": __builtins__}
    env.update({k: getattr(b3d, k) for k in dir(b3d) if not k.startswith("_")})
    env["math"] = __import__("math")
    if extra_globals:
        env.update(extra_globals)

    try:
        exec(compile(script, "<model_script>", "exec"), env)
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


def _export(shape, base: Path, formats: list[str]) -> list[str]:
    from build123d import Mesher, export_brep, export_gltf, export_step, export_stl

    written: list[str] = []
    for fmt in formats:
        fmt = fmt.lower().lstrip(".")
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
        shape = _import_any(path)
        solids = shape.solids()
        for i, s in enumerate(solids):
            label = getattr(s, "label", "") or None
            ref = f"{path.name}#{i}" + (f" ({label})" if label else "")
            entries.append({"ref": ref, "solid": s})
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


# ---------------------------------------------------------------------------
# modeling tools
# ---------------------------------------------------------------------------

@mcp.tool()
def create_model(name: str, script: str, formats: list[str] | None = None,
                 params_file: str | None = None) -> dict:
    """Create a 3D model from a build123d Python script and export it.

    The script runs with `from build123d import *` already in scope and MUST
    assign the final shape to a variable named `result`. Dimensions are in mm.

    Example script:
        with BuildPart() as p:
            Box(80, 40, 20)
            fillet(p.edges().filter_by(Axis.Z), radius=5)
            with Locations((0, 0, 10)):
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
    shape = _run_script(script, extra)
    base = WORKDIR / _safe_name(name)
    files = _export(shape, base, formats or ["step"])
    return {
        "files": files,
        "stats": _shape_stats(shape),
        "next_step": "Use open_in_shapr3d to load the STEP file into Shapr3D.",
    }


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
    shape = _run_script(script, extra)
    base = WORKDIR / _safe_name(output_name or src.stem + "_modified")
    files = _export(shape, base, formats or ["step"])
    return {"files": files, "stats": _shape_stats(shape)}


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
    return info


@mcp.tool()
def detect_clash(paths: list[str], min_overlap_mm3: float = 0.001) -> dict:
    """Check solids for interference (clash) by pairwise boolean intersection.

    Pass one file (all solids in it are checked against each other — e.g. an
    assembly compound) or several files (solids are pooled across them).
    Reference geometry like keep-out volumes can be modeled as solids and
    clash-checked against real parts to enforce interface contracts.

    Args:
        paths: One or more STEP/IGES/STL/BREP/3MF files. Relative paths
            resolve against the models workspace.
        min_overlap_mm3: Overlap volume below this is ignored (numeric noise
            from coincident faces). Default 0.001 mm^3.

    Returns:
        clear: True if no pair overlaps; clashes: list of overlapping pairs
        with their overlap volume and bounding box, so the offending region
        can be located.
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
    clashes = []
    check_errors = []
    pairs = 0
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            pairs += 1
            a, b = entries[i], entries[j]
            # Cheap reject: bounding boxes that don't intersect can't clash.
            ba, bb = a["solid"].bounding_box(), b["solid"].bounding_box()
            if (ba.min.X > bb.max.X or bb.min.X > ba.max.X or
                    ba.min.Y > bb.max.Y or bb.min.Y > ba.max.Y or
                    ba.min.Z > bb.max.Z or bb.min.Z > ba.max.Z):
                continue
            # A failed boolean must NOT read as "no clash" - record it so the
            # caller knows the check was incomplete.
            try:
                overlap = a["solid"].intersect(b["solid"])
                vol = (getattr(overlap, "volume", 0.0) or 0.0) if overlap else 0.0
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
                    obb = overlap.bounding_box()
                    entry["overlap_bbox_min"] = [
                        round(v, 3) for v in (obb.min.X, obb.min.Y, obb.min.Z)
                    ]
                    entry["overlap_bbox_max"] = [
                        round(v, 3) for v in (obb.max.X, obb.max.Y, obb.max.Z)
                    ]
                except Exception:
                    pass  # location is best-effort; the clash itself stands
                clashes.append(entry)
    return {
        "clear": not clashes and not check_errors,
        "solids": len(entries),
        "pairs_checked": pairs,
        "clashes": clashes,
        "check_errors": check_errors,
    }


@mcp.tool()
def convert_model(path: str, formats: list[str], output_name: str | None = None) -> dict:
    """Convert a CAD file to other formats. Reads STEP/IGES/STL/BREP/3MF and
    writes step, stl, 3mf, gltf, glb, brep (glTF is export-only). E.g.
    convert an STL mesh to STEP-wrapped geometry, or STEP to STL for 3D
    printing."""
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    shape = _import_any(src)
    base = WORKDIR / _safe_name(output_name or src.stem)
    return {"files": _export(shape, base, formats)}


@mcp.tool()
def render_preview(path: str, width: int = 800, height: int = 600) -> Image:
    """Render an isometric PNG preview of a CAD file (STEP/IGES/STL/BREP/3MF)
    without opening Shapr3D. Useful to visually verify a model you just
    created before sending it to the app."""
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    shape = _import_any(src)

    # Render via STL -> matplotlib (no GPU/display needed)
    import numpy as np

    with tempfile.TemporaryDirectory() as td:
        stl_path = Path(td) / "preview.stl"
        from build123d import export_stl
        export_stl(shape, str(stl_path))
        tris, normals = _read_binary_stl(stl_path)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    fig = plt.figure(figsize=(width / 100, height / 100), dpi=100)
    ax = fig.add_subplot(111, projection="3d")
    light = np.array([0.4, 0.3, 0.85])
    light = light / np.linalg.norm(light)
    shade = 0.35 + 0.65 * np.clip(normals @ light, 0, 1)
    colors = np.outer(shade, np.array([0.35, 0.55, 0.85]))
    coll = Poly3DCollection(tris, facecolors=colors, edgecolor="none")
    ax.add_collection3d(coll)
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    center, span = (lo + hi) / 2, (hi - lo).max() / 2 or 1
    ax.set_xlim(center[0] - span, center[0] + span)
    ax.set_ylim(center[1] - span, center[1] + span)
    ax.set_zlim(center[2] - span, center[2] + span)
    ax.set_axis_off()
    ax.view_init(elev=30, azim=-60)
    out = WORKDIR / (_safe_name(src.stem) + "_preview.png")
    fig.savefig(out, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return Image(path=str(out))


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
def open_in_shapr3d(path: str) -> str:
    """Open a CAD file in Shapr3D. STEP files import as editable solid
    bodies; Shapr3D shows an import dialog where the user confirms units.

    On Windows, Shapr3D (a Store app) only accepts .shapr files from other
    programs. For STEP and other formats this launches Shapr3D and puts the
    file's full path on the clipboard; the user then imports it in the app.
    """
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    if not IS_WINDOWS:
        subprocess.run(
            ["open", "-a", APP_NAME, str(src)], check=True, timeout=15,
            stdin=subprocess.DEVNULL,
        )
        return f"Sent {src.name} to Shapr3D. {IMPORT_DIALOG_NOTE}"
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
    host process the first time and captures the window's screen region,
    so keep it uncovered. On Windows, captures the window even when other
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
