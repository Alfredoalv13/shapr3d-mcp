"""Kernel-side modeling tools: guarded booleans, file builds with cached
imports, assembly inventory, labelled clash reports, feature queries,
sections and model diffs. None of these touch the Shapr3D app."""

import math
import os
from pathlib import Path

import pytest

from shapr3d_mcp import kernel, server

WORKDIR = Path(os.environ["SHAPR3D_MCP_WORKDIR"])


# ---------------------------------------------------------------------------
# guarded booleans
# ---------------------------------------------------------------------------

def test_cut_and_fuse_basic():
    from build123d import Box, Cylinder, Pos

    warn = []
    cut, fuse = kernel.make_cut(warn), kernel.make_fuse(warn)
    holed = cut(Box(10, 10, 10), Cylinder(2, 20))
    assert holed.volume == pytest.approx(1000 - math.pi * 4 * 10, rel=1e-4)
    grown = fuse(Box(10, 10, 10), Pos(7, 0, 0) * Box(4, 4, 4))  # x 5..9 sticks out
    assert grown.volume == pytest.approx(1000 + 4 * 4 * 4, rel=1e-4)
    assert warn == []


def test_cut_with_nested_compound_tool_really_cuts():
    # Regression: loft() returns a Part wrapping a Compound; subtracting it
    # directly silently did nothing in the VGC10 extension.
    from build123d import Box, Circle, Plane, Pos, loft

    block = Pos(0, 0, -15) * Box(40, 40, 30)
    tool = loft([(Plane.XY.offset(-2) * Circle(10)).face(),
                 (Plane.XY.offset(-28) * Circle(5)).face()])
    out = kernel.make_cut([])(block, tool)
    assert out.volume < block.volume - 1000


def test_cut_without_overlap_warns():
    from build123d import Box, Pos

    warn = []
    out = kernel.make_cut(warn)(Box(10, 10, 10), Pos(50, 0, 0) * Box(2, 2, 2))
    assert out.volume == pytest.approx(1000)
    assert warn and "does not overlap" in warn[0]


def test_fuse_inside_warns():
    from build123d import Box

    warn = []
    kernel.make_fuse(warn)(Box(10, 10, 10), Box(2, 2, 2))
    assert warn and "already inside" in warn[0]


class _Fake:
    """Stand-in solid whose boolean silently does nothing."""

    def __init__(self, volume, overlap):
        self.volume, self._overlap = volume, overlap

    def solids(self):
        return [self]

    def __and__(self, other):
        return _Fake(self._overlap, 0)

    def __sub__(self, other):
        return self  # the OpenCascade failure mode: unchanged result

    __add__ = __sub__


def test_silent_noop_boolean_raises():
    with pytest.raises(kernel.BooleanError, match="cut #1 failed"):
        kernel.make_cut([])(_Fake(1000, 100), _Fake(200, 100))
    with pytest.raises(kernel.BooleanError, match="fuse #1 failed"):
        kernel.make_fuse([])(_Fake(1000, 10), _Fake(200, 10))


def test_create_model_reports_boolean_warnings():
    out = server.create_model(
        "warn_box", "result = cut(Box(10, 10, 10), Pos(40, 0, 0) * Box(1, 1, 1))"
    )
    assert out["boolean_warnings"] and "does not overlap" in out["boolean_warnings"][0]


def test_boolean_error_surfaces_clearly(monkeypatch):
    def broken(warnings=None):
        def cut(*a):
            raise kernel.BooleanError("cut #1 failed: test")
        return cut
    monkeypatch.setattr(kernel, "make_cut", broken)
    with pytest.raises(RuntimeError, match="Boolean check failed: cut #1 failed"):
        server.create_model("broken_cut", "result = cut(Box(1, 1, 1), Box(1, 1, 1))")


# ---------------------------------------------------------------------------
# file-based builds + cached imports
# ---------------------------------------------------------------------------

def test_run_model_file_with_include_export_part_and_params(tmp_path):
    proj = WORKDIR / "proj_a"
    proj.mkdir(exist_ok=True)
    (proj / "params.yaml").write_text("plate: {w: 40, h: 4}\n")
    (proj / "plate.py").write_text(
        "P = params['plate']\n"
        "plate = Box(P['w'], P['w'], P['h'])\n"
        "plate.label = 'plate'\n"
    )
    (proj / "assembly.py").write_text(
        "include('plate.py')\n"
        "export_part(plate, 'proj_a_plate', ['step', 'stl'])\n"
        "pin = Pos(0, 0, 10) * Cylinder(2, 10)\n"
        "pin.label, pin.color = 'pin', Color(0.1, 0.1, 0.1)\n"
        "result = Compound(children=[plate, pin])\n"
    )
    out = server.run_model_file("proj_a/assembly.py", "proj_a_asm",
                                params_file=str(proj / "params.yaml"))
    names = [Path(f).name for f in out["files"]]
    assert names == ["proj_a_plate.step", "proj_a_plate.stl", "proj_a_asm.step"]
    assert all(Path(f).exists() for f in out["files"])
    assert [p["part"] for p in out["parts"]] == ["plate", "pin"]
    assert out["parts"][0]["volume_mm3"] == pytest.approx(40 * 40 * 4, rel=1e-4)


def test_script_print_does_not_touch_stdout(capsys):
    server.create_model("printy", "print('chatter from a model script')\nresult = Box(1, 1, 1)")
    cap = capsys.readouterr()
    assert cap.out == "" and "chatter" in cap.err


def test_script_own_step_import_keeps_stdout_clean():
    # A script calling build123d.import_step on a slightly broken vendor
    # file directly (not via load_cad) must not leak OCCT chatter either.
    import subprocess
    import sys

    from build123d import Box, export_step

    src = WORKDIR / "own_noisy_src.step"
    export_step(Box(5, 5, 5), str(src))
    noisy = WORKDIR / "own_noisy.step"
    noisy.write_text(src.read_text().replace(
        "ENDSEC;\nEND-ISO", "#999990 = CARTESIAN_POINT ( 'bad', #888888 ) ;\nENDSEC;\nEND-ISO"))
    code = (
        "from shapr3d_mcp import server\n"
        f"server.create_model('own_noisy_out', \"result = import_step({str(noisy)!r})\")\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          env={**os.environ, "SHAPR3D_MCP_WORKDIR": str(WORKDIR)}, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout == b"", proc.stdout


def test_run_model_file_missing():
    with pytest.raises(FileNotFoundError):
        server.run_model_file("nope/does_not_exist.py", "x")


def test_load_cad_caches_step(monkeypatch):
    server.create_model("vendor_block", "result = Box(12, 12, 12)")
    calls = []
    real = server._import_any
    monkeypatch.setattr(server, "_import_any", lambda p: calls.append(p) or real(p))
    script = "a = load_cad('vendor_block.step')\nb = load_cad('vendor_block.step')\nresult = a"
    out = server.create_model("vendor_user", script)
    assert out["stats"]["volume_mm3"] == pytest.approx(1728, rel=1e-4)
    assert len(calls) == 1  # second load came from the BREP cache
    assert list((WORKDIR / ".cache").glob("vendor_block-*.brep"))


def test_load_cad_cache_invalidated_on_change(monkeypatch):
    server.create_model("vendor_change", "result = Box(5, 5, 5)")
    s1 = "result = load_cad('vendor_change.step')"
    assert server.create_model("vc1", s1)["stats"]["volume_mm3"] == pytest.approx(125)
    server.create_model("vendor_change", "result = Box(6, 6, 6)")  # new mtime/size
    assert server.create_model("vc2", s1)["stats"]["volume_mm3"] == pytest.approx(216)


# ---------------------------------------------------------------------------
# inventory + labelled clash report
# ---------------------------------------------------------------------------

ASM = (
    "base = Box(40, 40, 10); base.label = 'Base'\n"
    "screw = Pos(10, 0, 6) * Cylinder(2.5, 14); screw.label = 'Screw'\n"
    "lid = Pos(0, 0, 5.5) * Box(30, 30, 1); lid.label = 'Lid'\n"
    "far = Pos(0, 25.4, 0) * Box(10, 10, 10); far.label = 'Bracket'\n"
    "result = Compound(children=[base, screw, lid, far])\n"
)


def test_inspect_lists_parts():
    server.create_model("asm_inv", ASM)
    info = server.inspect_model("asm_inv.step")
    assert [p["part"] for p in info["parts"]] == ["Base", "Screw", "Lid", "Bracket"]
    assert info["parts"][0]["volume_mm3"] == pytest.approx(16000, rel=1e-4)


def test_unlabelled_children_get_positional_names():
    server.create_model("asm_plain", "result = Compound(children=[Box(5,5,5), Pos(9,0,0)*Box(5,5,5)])")
    info = server.inspect_model("asm_plain.step")
    assert "parts" not in info  # nothing meaningful to name


def test_clash_names_parts_and_splits_expected():
    server.create_model("asm_clash", ASM)
    rep = server.detect_clash(["asm_clash.step"], expected=["Screw|Base"],
                              near_miss_mm=1.0)
    clash_pairs = {(c["a"], c["b"]) for c in rep["clashes"]}
    assert ("Screw#0", "Lid#0") in clash_pairs
    assert rep["expected_overlaps"][0]["a"] == "Base#0"
    assert rep["clear"] is False
    gaps = {(n["a"], n["b"]): n["gap_mm"] for n in rep["near_misses"]}
    assert gaps[("Base#0", "Bracket#0")] == pytest.approx(0.4, abs=1e-3)


def test_near_miss_measures_real_gap_inside_overlapping_boxes():
    # A pin in a ring: bounding boxes overlap, solids are 0.3 mm apart.
    server.create_model(
        "asm_ring",
        "ring = Cylinder(10, 5) - Cylinder(5.3, 6); ring.label = 'Ring'\n"
        "pin = Cylinder(5, 8); pin.label = 'Pin'\n"
        "far = Pos(0, 0, 30) * Box(4, 4, 4); far.label = 'Far'\n"
        "result = Compound(children=[ring, pin, far])\n",
    )
    rep = server.detect_clash(["asm_ring.step"], near_miss_mm=0.5)
    assert rep["clear"] is True
    assert rep["near_misses"] == [{"a": "Ring#0", "b": "Pin#0", "gap_mm": pytest.approx(0.3, abs=1e-3)}]


def test_clash_clear_when_only_expected():
    server.create_model("asm_ok", ASM.replace("lid = Pos(0, 0, 5.5)", "lid = Pos(0, 0, 30)"))
    rep = server.detect_clash(["asm_ok.step"], expected=["Screw"])
    assert rep["clear"] is True and rep["expected_overlaps"]


@pytest.mark.parametrize("pat,a,b,hit", [
    ("screw|base", "Base#0", "Screw#1", True),
    ("screw|base", "Base#0", "Lid#0", False),
    ("hose", "6 mm hoses#0", "Y#0", True),
    # STEP export mangles labels; patterns written with spaces must still hit
    ("bottom fitting|Extension bottom", "Extension_bottom_half#0",
     "IQSG_186_G_bottom_fitting__approx_#0", True),
])
def test_pair_matches(pat, a, b, hit):
    assert kernel.pair_matches(pat, a, b) is hit


# ---------------------------------------------------------------------------
# feature queries
# ---------------------------------------------------------------------------

def test_query_features_finds_hole_and_boss():
    server.create_model(
        "feat_part",
        "b = Box(30, 30, 10) - Pos(-8, 0, 0) * Cylinder(3, 20)\n"
        "b += Pos(8, 0, 8) * Cylinder(4, 6)\n"
        "result = b",
    )
    out = server.query_features("feat_part.step", kinds=["cylinder"])
    cyl = {(f["kind"], f["radius"]) for f in out["features"]}
    assert ("hole", 3.0) in cyl and ("boss", 4.0) in cyl
    hole = next(f for f in out["features"] if f["kind"] == "hole")
    assert abs(hole["axis"][2]) == 1 and hole["length"] == pytest.approx(10, abs=1e-3)


def test_query_features_planes_region_and_part():
    server.create_model("feat_asm", ASM)
    out = server.query_features("feat_asm.step", part="Lid", kinds=["plane"])
    zs = sorted(f["center"][2] for f in out["features"] if abs(f["normal"][2]) == 1)
    assert zs == pytest.approx([5.0, 6.0])
    top = server.query_features("feat_asm.step", part="Base", kinds=["plane"],
                                region_min=[-50, -50, 4.9], region_max=[50, 50, 5.1])
    horizontal = [f for f in top["features"] if abs(f["normal"][2]) == 1]
    assert [f["center"][2] for f in horizontal] == pytest.approx([5.0])
    assert all(f["center"][2] != pytest.approx(-5.0) for f in top["features"])
    with pytest.raises(ValueError, match="No solids match"):
        server.query_features("feat_asm.step", part="Nonexistent")


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def test_section_area_matches_geometry():
    from build123d import Box, Cylinder

    part = Box(20, 20, 10) - Cylinder(3, 20)
    faces = kernel.section_faces(part, kernel.section_plane("XZ", 0), 200)
    assert sum(f.area for f in faces) == pytest.approx(20 * 10 - 6 * 10, rel=1e-4)


def test_section_view_png_and_miss():
    server.create_model("sec_part", ASM)
    server.section_view("sec_part.step", plane="XZ", offset=0)
    assert (WORKDIR / "sec_part_section_XZ_0.png").stat().st_size > 2000
    with pytest.raises(ValueError, match="does not cut"):
        server.section_view("sec_part.step", plane="XY", offset=500)


def test_face_rings_keep_sharp_corners():
    from build123d import Box

    pl = kernel.section_plane("XZ", 0)
    face = kernel.section_faces(Box(20, 20, 10), pl, 200)[0]
    (ring,) = kernel.face_rings(face, pl)
    assert sorted((round(x, 6), round(y, 6)) for x, y in ring) == [
        (-10, -5), (-10, 5), (10, -5), (10, 5)]


def test_face_rings_orientation():
    from build123d import Box, Cylinder

    pl = kernel.section_plane("XY", 0)
    face = kernel.section_faces(Box(20, 20, 10) - Cylinder(3, 20), pl, 200)[0]
    outer, hole = kernel.face_rings(face, pl)
    area = lambda r: 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1]))
    assert area(outer) > 0 > area(hole)


# ---------------------------------------------------------------------------
# diff (round-trip through Shapr3D)
# ---------------------------------------------------------------------------

def test_diff_detects_chamfer():
    server.create_model("diff_before", "result = Box(20, 20, 10)")
    server.create_model(
        "diff_after",
        "with BuildPart() as p:\n"
        "    Box(20, 20, 10)\n"
        "    chamfer(p.edges().group_by(Axis.Z)[-1].sort_by(Axis.X)[-1:], 2)\n"
        "result = p.part",
    )
    d = server.diff_models("diff_before.step", "diff_after.step")
    assert d["volume_change_mm3"] == pytest.approx(-0.5 * 2 * 2 * 20, rel=1e-3)
    assert d["added_regions"] == [] and len(d["removed_regions"]) == 1
    region = d["removed_regions"][0]
    assert region["min"][0] == pytest.approx(8) and region["min"][2] == pytest.approx(3)
    slanted = [f for f in d["new_faces"] if f["type"] == "plane"
               and 0.1 < abs(f["normal"][2]) < 0.9]
    assert len(slanted) == 1


def test_diff_identical_and_part_filter():
    server.create_model("diff_same_a", ASM)
    server.create_model("diff_same_b", ASM)
    assert server.diff_models("diff_same_a.step", "diff_same_b.step", part="Lid")["identical"]
    with pytest.raises(ValueError, match="No solids match"):
        server.diff_models("diff_same_a.step", "diff_same_b.step", part="Gear")


# ---------------------------------------------------------------------------
# diff robustness (round-trips through Shapr3D's kernel)
# ---------------------------------------------------------------------------

def test_same_shape_ignores_kernel_noise():
    from build123d import Box, Pos

    a = Box(20, 20, 10)
    assert kernel.same_shape(a, Pos(1e-7, 0, 0) * Box(20, 20, 10))
    assert not kernel.same_shape(a, Pos(0.5, 0, 0) * Box(20, 20, 10))  # moved
    assert not kernel.same_shape(a, Box(20, 20, 10.2))                 # resized
    d = kernel.diff_solids(a, Pos(1e-7, 0, 0) * Box(20, 20, 10))
    assert d["identical"] and d["method"] == "mass properties"


def test_cancel_artifact_pairs():
    whole = {"volume_mm3": 1000.0, "min": [0, 0, 0], "max": [10, 10, 10]}
    real = {"volume_mm3": 5.0, "min": [8, 8, 8], "max": [10, 10, 10]}
    removed, added, n = kernel.cancel_artifact_pairs([whole, real], [dict(whole, volume_mm3=1000.4)])
    assert removed == [real] and added == [] and n == 1


def test_sampled_regions_approximate_chamfer():
    from build123d import Axis, Box, BuildPart, chamfer

    with BuildPart() as p:
        Box(20, 20, 10)
        chamfer(p.edges().group_by(Axis.Z)[-1].sort_by(Axis.X)[-1:], 3)
    removed, added, step = kernel.sampled_regions(Box(20, 20, 10), p.part)
    assert added == [] and len(removed) == 1
    assert removed[0]["volume_mm3"] == pytest.approx(0.5 * 9 * 20, rel=0.35)
    assert removed[0]["min"][0] > 5  # at the +X top edge


def test_diff_ignores_sliver_regions():
    from build123d import Box, Pos

    # 1 micron overlap difference -> a sliver, not a change worth reporting
    d = kernel.diff_solids(Box(20, 20, 10), Pos(0, 0, 0.001) * Box(20, 20, 10))
    assert all(r["volume_mm3"] > 0.01 for r in d["removed_regions"] + d["added_regions"])


def test_inventory_descends_wrapper_group():
    from build123d import Box, Compound, Pos

    a, b = Box(5, 5, 5), Pos(10, 0, 0) * Box(5, 5, 5)
    a.label, b.label = "Left", "Right"
    wrapped = Compound(children=[Compound(children=[a, b], label="export.step")])
    assert [p["part"] for p in kernel.part_inventory(wrapped)] == ["Left", "Right"]
    assert [e["ref"] for e in kernel.labelled_solids(wrapped, "f")] == ["Left#0", "Right#0"]


# ---------------------------------------------------------------------------
# progress images
# ---------------------------------------------------------------------------

def test_quick_look_writes_sheet_and_filters():
    server.create_model("ql_asm", ASM)
    out = server.quick_look("ql_asm.step", exclude=["Bracket"])
    assert Path(out["image"]).stat().st_size > 10_000
    assert "Bracket" not in out["parts"] and "Base" in out["parts"]
    with pytest.raises(ValueError, match="No solids left"):
        server.quick_look("ql_asm.step", include=["Gearbox"])


def test_section_zoom_writes_separate_file():
    server.create_model("zoom_part", ASM)
    server.section_view("zoom_part.step", plane="XZ", offset=0,
                        zoom_min=[-5, -6], zoom_max=[15, 8])
    assert (WORKDIR / "zoom_part_section_XZ_0_zoom.png").exists()
