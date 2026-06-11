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


def _osascript(script: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=15
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# modeling tools
# ---------------------------------------------------------------------------

@mcp.tool()
def create_model(name: str, script: str, formats: list[str] | None = None) -> dict:
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
    """
    shape = _run_script(script)
    base = WORKDIR / _safe_name(name)
    files = _export(shape, base, formats or ["step"])
    return {
        "files": files,
        "stats": _shape_stats(shape),
        "next_step": "Use open_in_shapr3d to load the STEP file into Shapr3D.",
    }


@mcp.tool()
def modify_model(input_path: str, script: str, output_name: str | None = None,
                 formats: list[str] | None = None) -> dict:
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
    """
    src = _resolve(input_path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    imported = _import_any(src)
    shape = _run_script(script, {"imported": imported})
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
# Shapr3D app bridge (macOS)
# ---------------------------------------------------------------------------

@mcp.tool()
def open_in_shapr3d(path: str) -> str:
    """Open a CAD file in Shapr3D. STEP files import as editable solid
    bodies; Shapr3D shows an import dialog where the user confirms units."""
    src = _resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"No such file: {src}")
    subprocess.run(["open", "-a", APP_NAME, str(src)], check=True, timeout=15)
    return (
        f"Sent {src.name} to Shapr3D. The app shows an import confirmation; "
        "the user may need to click Import and choose units (files are in mm)."
    )


@mcp.tool()
def shapr3d_status() -> dict:
    """Check whether Shapr3D is installed, running, and frontmost."""
    installed = Path(f"/Applications/{APP_NAME}.app").exists()
    running = subprocess.run(
        ["pgrep", "-x", APP_NAME], capture_output=True
    ).returncode == 0
    frontmost = None
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
    subprocess.run(["open", "-a", APP_NAME], check=True, timeout=15)
    return "Shapr3D activated."


@mcp.tool()
def screenshot_shapr3d() -> Image:
    """Capture a screenshot of the Shapr3D window to see the current state
    of the model/app. Requires Screen Recording permission for the host
    process the first time."""
    if subprocess.run(["pgrep", "-x", APP_NAME], capture_output=True).returncode != 0:
        raise RuntimeError("Shapr3D is not running. Call activate_shapr3d first.")
    try:
        bounds = _osascript(
            f'tell application "System Events" to tell process "{APP_NAME}" to '
            "get {position, size} of front window"
        )
        x, y, w, h = [int(v.strip()) for v in bounds.split(",")]
        region = ["-R", f"{x},{y},{w},{h}"]
    except Exception:
        region = []  # fall back to full screen
    out = WORKDIR / "_shapr3d_screenshot.png"
    subprocess.run(
        ["screencapture", "-x", *region, str(out)], check=True, timeout=15
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
