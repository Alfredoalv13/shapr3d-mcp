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

from shapr3d_mcp import kernel

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
    text = src.read_text()
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
        ["osascript", "-e", script], capture_output=True, text=True, timeout=15
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout.strip()


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
        f"Sent {src.name} to Shapr3D. The app shows an Import Preferences "
        "dialog (Quality/Speed/Custom); the user clicks Import. STEP files "
        "carry their units (mm), so no unit choice is needed."
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
