"""CAD-kernel helpers shared by the modeling tools (no Shapr3D needed).

- Guarded booleans (`cut`, `fuse`) that catch OpenCascade's silent no-ops.
- `load_cad` with a BREP cache so slow vendor STEP files import once.
- Assembly inventory, feature queries and planar sections.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path


class BooleanError(RuntimeError):
    """A boolean returned a result inconsistent with the overlap it had."""


# ---------------------------------------------------------------------------
# guarded booleans
# ---------------------------------------------------------------------------

def solidify(shape):
    """Bare Solid (or a flat Compound of Solids) from any shape.

    loft()/extrude() in algebra mode return a Part wrapping a Compound;
    OpenCascade booleans against such nested compounds can silently do
    nothing, so tools are flattened to their solids first.
    """
    from build123d import Compound

    solids = list(shape.solids())
    if not solids:
        raise BooleanError(f"{type(shape).__name__} has no solids to boolean with")
    return solids[0] if len(solids) == 1 else Compound(children=solids)


def _vol(shape) -> float:
    if shape is None:
        return 0.0
    if isinstance(shape, list):
        return sum(_vol(s) for s in shape)
    return getattr(shape, "volume", 0.0) or 0.0


def _boolean(op: str, target, tools, warnings: list[str] | None):
    out = solidify(target)
    for k, tool in enumerate(tools):
        tool = solidify(tool)
        before = out.volume
        overlap = _vol(out & tool)
        new = out - tool if op == "cut" else out + tool
        new = solidify(new)
        change = new.volume - before
        expected = -overlap if op == "cut" else tool.volume - overlap
        scale = max(abs(expected), 1e-9)
        tag = f"{op} #{k + 1}"
        if abs(expected) < 1e-6 * max(before, 1.0):
            # nothing to do: tool in empty space (cut) or fully inside (fuse)
            if warnings is not None:
                where = "does not overlap the part" if op == "cut" else "is already inside the part"
                warnings.append(f"{tag}: tool {where}; result unchanged")
        elif abs(change - expected) > 0.02 * scale + 1e-6:
            raise BooleanError(
                f"{tag} failed: volume changed by {change:.3f} mm3 but the "
                f"overlap implies {expected:.3f} mm3. Typical cause: faces "
                "within ~0.01 mm of each other; fuse the tool pieces on an "
                "exact shared face or move them clearly apart."
            )
        out = new
    return out


def make_cut(warnings: list[str] | None = None):
    def cut(target, *tools):
        """target minus tools, verified against the actual overlap."""
        return _boolean("cut", target, tools, warnings)
    return cut


def make_fuse(warnings: list[str] | None = None):
    def fuse(target, *tools):
        """target plus tools, verified against the actual overlap."""
        return _boolean("fuse", target, tools, warnings)
    return fuse


# ---------------------------------------------------------------------------
# cached CAD import
# ---------------------------------------------------------------------------

def cache_key(path: Path) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def load_cad(path: Path, importer, cache_dir: Path, use_cache: bool = True):
    """Import a CAD file; STEP/IGES results are cached as BREP keyed by
    path + mtime + size (labels/colours are not kept in the cache)."""
    from build123d import export_brep, import_brep

    if not use_cache or path.suffix.lower() not in {".step", ".stp", ".iges", ".igs"}:
        return importer(path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{path.stem}-{cache_key(path)}.brep"
    if cached.exists():
        return import_brep(str(cached))
    shape = importer(path)
    export_brep(shape, str(cached))
    return shape


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

def _bbox(shape) -> dict:
    bb = shape.bounding_box()
    return {"min": [round(v, 2) for v in (bb.min.X, bb.min.Y, bb.min.Z)],
            "max": [round(v, 2) for v in (bb.max.X, bb.max.Y, bb.max.Z)]}


GENERIC_LABELS = {"", "assembly"}  # what STEP import gives unnamed children


def unwrap_groups(shape):
    """Descend through single-child wrapper groups (Shapr3D exports put the
    whole assembly under one top-level group named after the file)."""
    while True:
        children = list(getattr(shape, "children", []) or [])
        if len(children) == 1 and list(getattr(children[0], "children", []) or []):
            shape = children[0]
        else:
            return shape


def child_names(shape, file_tag: str) -> list[str] | None:
    """Names for an assembly's top-level children, or None if it has no
    real labels. Generic / repeated labels get a positional suffix."""
    children = list(getattr(shape, "children", []) or [])
    labels = [(getattr(c, "label", "") or "").strip() for c in children]
    if not children or all(l.lower() in GENERIC_LABELS for l in labels):
        return None
    names = []
    for i, l in enumerate(labels):
        if l.lower() in GENERIC_LABELS:
            names.append(f"{file_tag}[{i}]")
        elif labels.count(l) > 1:
            names.append(f"{l}[{i}]")
        else:
            names.append(l)
    return names


def part_inventory(shape) -> list[dict]:
    """Top-level assembly children with solid counts, volume and bbox.
    Empty when the shape is not an assembly with labelled children."""
    shape = unwrap_groups(shape)
    names = child_names(shape, "part")
    if not names:
        return []
    parts = []
    for name, c in zip(names, shape.children):
        solids = list(c.solids())
        parts.append({
            "part": name,
            "solids": len(solids),
            "volume_mm3": round(sum(s.volume for s in solids), 1),
            **_bbox(c),
        })
    return parts


def labelled_solids(shape, file_tag: str) -> list[dict]:
    """[{ref, part, solid}] - refs use assembly child labels when present."""
    shape = unwrap_groups(shape)
    names = child_names(shape, file_tag)
    out = []
    if names:
        for part, c in zip(names, shape.children):
            for si, s in enumerate(c.solids()):
                out.append({"ref": f"{part}#{si}", "part": part, "solid": s})
        return out
    for si, s in enumerate(shape.solids()):
        label = getattr(s, "label", "") or None
        ref = f"{file_tag}#{si}" + (f" ({label})" if label else "")
        out.append({"ref": ref, "part": label or file_tag, "solid": s})
    return out


def bbox_gap(a, b) -> float:
    """Distance between two axis-aligned bounding boxes (0 if overlapping)."""
    A, B = a.bounding_box(), b.bounding_box()
    d = [max(0.0, B.min.X - A.max.X, A.min.X - B.max.X),
         max(0.0, B.min.Y - A.max.Y, A.min.Y - B.max.Y),
         max(0.0, B.min.Z - A.max.Z, A.min.Z - B.max.Z)]
    return math.sqrt(sum(v * v for v in d))


def norm(text: str) -> str:
    """Lowercase, punctuation/underscores -> single spaces. STEP export turns
    'IQSG 186 G (approx)' into 'IQSG_186_G__approx_'; both normalise alike."""
    import re

    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


def pair_matches(pattern: str, ref_a: str, ref_b: str) -> bool:
    """'A|B' matches a pair whose refs contain A and B (either order,
    case/punctuation-insensitive); without '|' either ref may contain it."""
    a, b = norm(ref_a), norm(ref_b)
    if "|" in pattern:
        p, q = (norm(x) for x in pattern.split("|", 1))
        return (p in a and q in b) or (p in b and q in a)
    p = norm(pattern)
    return p in a or p in b


# ---------------------------------------------------------------------------
# feature queries
# ---------------------------------------------------------------------------

def _in_region(bb, region) -> bool:
    if not region:
        return True
    lo, hi = region
    return not (bb.max.X < lo[0] or bb.min.X > hi[0] or bb.max.Y < lo[1] or
                bb.min.Y > hi[1] or bb.max.Z < lo[2] or bb.min.Z > hi[2])


def cylinder_info(face) -> dict:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopAbs import TopAbs_REVERSED

    cyl = BRepAdaptor_Surface(face.wrapped).Cylinder()
    ax = cyl.Axis()
    d, o = ax.Direction(), ax.Location()
    direction = [d.X(), d.Y(), d.Z()]
    origin = [o.X(), o.Y(), o.Z()]
    # extent along the axis from the face vertices / bbox corners
    bb = face.bounding_box()
    corners = [(x, y, z) for x in (bb.min.X, bb.max.X) for y in (bb.min.Y, bb.max.Y)
               for z in (bb.min.Z, bb.max.Z)]
    ts = [sum((c[i] - origin[i]) * direction[i] for i in range(3)) for c in corners]
    # normals of a REVERSED cylindrical face point at the axis -> hole
    concave = face.wrapped.Orientation() == TopAbs_REVERSED
    # project axis origin to the start of the extent for a readable location
    t0 = min(ts)
    start = [origin[i] + t0 * direction[i] for i in range(3)]
    return {
        "radius": round(cyl.Radius(), 3),
        "diameter": round(2 * cyl.Radius(), 3),
        "axis": [round(v, 3) + 0.0 for v in direction],
        "axis_point": [round(v, 2) + 0.0 for v in start],
        "length": round(max(ts) - t0, 2),
        "kind": "hole" if concave else "boss",
    }


def plane_info(face) -> dict:
    c = face.center()
    n = face.normal_at(c)
    return {
        "normal": [round(n.X, 3) + 0.0, round(n.Y, 3) + 0.0, round(n.Z, 3) + 0.0],
        "center": [round(c.X, 2) + 0.0, round(c.Y, 2) + 0.0, round(c.Z, 2) + 0.0],
        "area_mm2": round(face.area, 2),
    }


def query_features(entries: list[dict], kinds: set[str], region=None,
                   min_area: float = 0.0, max_results: int = 80) -> list[dict]:
    from build123d import GeomType

    seen, out = set(), []
    for e in entries:
        for f in e["solid"].faces():
            if not _in_region(f.bounding_box(), region):
                continue
            if f.geom_type == GeomType.CYLINDER and "cylinder" in kinds:
                info = {"part": e["ref"], "type": "cylinder", **cylinder_info(f)}
                key = ("c", e["ref"], info["radius"], tuple(info["axis_point"]), info["length"])
            elif f.geom_type == GeomType.PLANE and "plane" in kinds and f.area >= min_area:
                info = {"part": e["ref"], "type": "plane", **plane_info(f)}
                key = ("p", e["ref"], tuple(info["normal"]), tuple(info["center"]))
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(info)
            if len(out) >= max_results:
                return out
    return out


# ---------------------------------------------------------------------------
# planar sections
# ---------------------------------------------------------------------------

PLANES = {"XY": ("XY", 2), "XZ": ("XZ", 1), "YZ": ("YZ", 0)}


def section_plane(name: str, offset: float):
    from build123d import Plane

    if name.upper() not in PLANES:
        raise ValueError(f"plane must be one of {sorted(PLANES)}")
    base = getattr(Plane, name.upper())
    return base.offset(offset)


def section_faces(solid, plane, size: float):
    """Faces of `solid` cut by `plane` (a large planar face intersection)."""
    from build123d import Face

    cutter = plane * Face.make_rect(size, size)
    r = solid & cutter
    if r is None:
        return []
    faces = r if isinstance(r, list) else [r]
    out = []
    for item in faces:
        out.extend(item.faces())
    return out


def face_rings(face, plane, samples: int = 160) -> list[list[tuple[float, float]]]:
    """Outer + inner wires of a planar face as 2-D rings in plane coords;
    outer counter-clockwise, holes clockwise (for non-zero fill)."""
    from build123d import Edge, GeomType
    from OCP.BRepTools import BRepTools_WireExplorer

    rings = []
    wires = [face.outer_wire()] + list(face.inner_wires())
    for k, w in enumerate(wires):
        # walk edges in wire order (through every vertex, so sharp corners
        # stay sharp); lines need only their start point
        pts = []
        explorer = BRepTools_WireExplorer(w.wrapped, face.wrapped)
        while explorer.More():
            e = Edge(explorer.Current())
            n = 1 if e.geom_type == GeomType.LINE else max(8, samples // 8)
            # position_at already follows the edge's orientation in the wire
            pts += [plane.to_local_coords(e.position_at(i / n)) for i in range(n)]
            explorer.Next()
        ring = [(p.X, p.Y) for p in pts]
        area = 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]))
        if (k == 0 and area < 0) or (k > 0 and area > 0):
            ring.reverse()
        rings.append(ring)
    return rings


# ---------------------------------------------------------------------------
# model diff (e.g. what the user changed in Shapr3D)
# ---------------------------------------------------------------------------

def _face_sig(face) -> tuple:
    c = face.center()
    return (face.geom_type.name, round(c.X, 1), round(c.Y, 1), round(c.Z, 1),
            round(face.area, 1))


def mass_props(solid) -> dict:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid.wrapped, g)
    c, m = g.CentreOfMass(), g.MatrixOfInertia()
    return {"volume": g.Mass(), "centroid": (c.X(), c.Y(), c.Z()),
            "inertia": (m.Value(1, 1), m.Value(2, 2), m.Value(3, 3))}


def same_shape(a, b, rel: float = 1e-4) -> bool:
    """Equal volume, centroid and inertia: the same solid even if another
    CAD kernel re-split or re-typed its faces (Shapr3D/Parasolid exports)."""
    pa, pb = mass_props(a), mass_props(b)
    v = max(pa["volume"], 1e-9)
    if abs(pa["volume"] - pb["volume"]) > max(rel * v, 0.5):
        return False
    size = max(a.bounding_box().diagonal, 1e-9)
    if math.dist(pa["centroid"], pb["centroid"]) > 1e-4 * size + 1e-3:
        return False
    return all(abs(x - y) <= 1e-3 * max(abs(x), 1e-9) for x, y in zip(pa["inertia"], pb["inertia"]))


def cancel_artifact_pairs(removed: list[dict], added: list[dict]) -> tuple[list, list, int]:
    """Drop removed/added region pairs with the same volume and bbox - the
    signature of a boolean that failed on near-coincident faces (it 'removes'
    the whole solid and 'adds' it back)."""
    removed, added, dropped = list(removed), list(added), 0
    for r in list(removed):
        for q in added:
            if (abs(r["volume_mm3"] - q["volume_mm3"]) <= 1e-3 * max(r["volume_mm3"], 1)
                    and all(abs(x - y) < 0.05 for x, y in zip(r["min"] + r["max"], q["min"] + q["max"]))):
                removed.remove(r)
                added.remove(q)
                dropped += 1
                break
    return removed, added, dropped


def sampled_regions(a, b, step: float | None = None) -> tuple[list, list, float]:
    """Point-sample both solids on a grid; cells inside only one of them
    form removed / added regions (approximate, kernel-independent)."""
    import numpy as np
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN

    bb_a, bb_b = a.bounding_box(), b.bounding_box()
    lo = [min(getattr(bb_a.min, k), getattr(bb_b.min, k)) for k in "XYZ"]
    hi = [max(getattr(bb_a.max, k), getattr(bb_b.max, k)) for k in "XYZ"]
    if step is None:
        # ~30k cells keeps this to seconds (each cell is one classifier call)
        box_vol = math.prod(max(h - l, 1e-3) for l, h in zip(lo, hi))
        step = max((box_vol / 30000) ** (1 / 3), 0.25)
    axes = [np.arange(l + step / 2, h, step) for l, h in zip(lo, hi)]
    shape = tuple(len(x) for x in axes)

    def inside(solid):
        cls = BRepClass3d_SolidClassifier(solid.wrapped)
        grid = np.zeros(shape, dtype=bool)
        for i, x in enumerate(axes[0]):
            for j, y in enumerate(axes[1]):
                for k, z in enumerate(axes[2]):
                    cls.Perform(gp_Pnt(x, y, z), 1e-6)
                    grid[i, j, k] = cls.State() == TopAbs_IN
        return grid

    ia, ib = inside(a), inside(b)

    def regions(mask):
        seen = np.zeros_like(mask)
        out = []
        for start in zip(*np.nonzero(mask)):
            if seen[start]:
                continue
            stack, cells = [start], []
            seen[start] = True
            while stack:
                c = stack.pop()
                cells.append(c)
                for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    n = (c[0] + d[0], c[1] + d[1], c[2] + d[2])
                    if all(0 <= n[t] < shape[t] for t in range(3)) and mask[n] and not seen[n]:
                        seen[n] = True
                        stack.append(n)
            idx = np.array(cells)
            mn = [float(axes[t][idx[:, t].min()] - step / 2) for t in range(3)]
            mx = [float(axes[t][idx[:, t].max()] + step / 2) for t in range(3)]
            out.append({"volume_mm3": round(len(cells) * step ** 3, 1),
                        "min": [round(v, 1) for v in mn], "max": [round(v, 1) for v in mx]})
        return sorted(out, key=lambda r: -r["volume_mm3"])

    return regions(ia & ~ib), regions(ib & ~ia), step


def describe_face(face) -> dict:
    from build123d import GeomType

    info = {"type": face.geom_type.name.lower(), "area_mm2": round(face.area, 2)}
    if face.geom_type == GeomType.PLANE:
        info.update(plane_info(face))
    elif face.geom_type == GeomType.CYLINDER:
        info.update(cylinder_info(face))
    else:
        c = face.center()
        info["center"] = [round(c.X, 2), round(c.Y, 2), round(c.Z, 2)]
    return info


def _regions(shape, min_volume: float) -> list[dict]:
    if shape is None:
        return []
    items = shape if isinstance(shape, list) else [shape]
    out = []
    for it in items:
        for s in it.solids():
            if s.volume < min_volume:
                continue
            bb = s.bounding_box()
            if s.volume < 1e-5 * max(bb.size.X * bb.size.Y * bb.size.Z, 1e-9):
                continue  # sliver from near-coincident faces, not a real change
            out.append({"volume_mm3": round(s.volume, 3), **_bbox(s)})
    return sorted(out, key=lambda r: -r["volume_mm3"])


def _touches(face, regions, pad: float = 1.0) -> bool:
    bb = face.bounding_box()
    for r in regions:
        if not (bb.max.X < r["min"][0] - pad or bb.min.X > r["max"][0] + pad or
                bb.max.Y < r["min"][1] - pad or bb.min.Y > r["max"][1] + pad or
                bb.max.Z < r["min"][2] - pad or bb.min.Z > r["max"][2] + pad):
            return True
    return False


def diff_solids(a, b, min_volume: float = 0.01, max_faces: int = 30) -> dict:
    """What changed from a to b: material added/removed (regions with
    bounding boxes) and the new/removed faces inside those regions.

    Robust to round-trips through another CAD kernel: equal mass properties
    count as unchanged, failed-boolean artifacts are cancelled, and if the
    boolean result still doesn't add up it falls back to point sampling."""
    A, B = solidify(a), solidify(b)
    base = {"volume_before_mm3": round(A.volume, 3), "volume_after_mm3": round(B.volume, 3),
            "volume_change_mm3": round(B.volume - A.volume, 3)}
    if same_shape(A, B):
        return {**base, "identical": True, "method": "mass properties",
                "removed_regions": [], "added_regions": [], "new_faces": [], "removed_faces": []}
    removed = _regions(A - B, min_volume)
    added = _regions(B - A, min_volume)
    removed, added, artifacts = cancel_artifact_pairs(removed, added)
    method = "boolean"
    net = sum(r["volume_mm3"] for r in added) - sum(r["volume_mm3"] for r in removed)
    dv = B.volume - A.volume
    if abs(net - dv) > 0.05 * max(abs(dv), 1.0) + 0.5:
        removed, added, step = sampled_regions(A, B)
        method = f"sampled ({step:.2f} mm grid, approximate)"
    changed = removed + added
    sig_a = {_face_sig(f): f for f in A.faces()}
    sig_b = {_face_sig(f): f for f in B.faces()}
    new_faces = [describe_face(f) for sg, f in sig_b.items() if sg not in sig_a and _touches(f, changed)]
    gone_faces = [describe_face(f) for sg, f in sig_a.items() if sg not in sig_b and _touches(f, changed)]
    out = {**base, "identical": not changed, "method": method,
           "removed_regions": removed, "added_regions": added,
           "new_faces": new_faces[:max_faces], "removed_faces": gone_faces[:max_faces]}
    if artifacts:
        out["cancelled_artifacts"] = artifacts
    return out
