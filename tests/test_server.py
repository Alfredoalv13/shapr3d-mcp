"""Tests for the Shapr3D MCP server.

These exercise the tool functions directly (no MCP transport, no Shapr3D
app). The workspace is a temp directory set up in conftest.py.
"""

import os
from pathlib import Path

import pytest

from shapr3d_mcp import server

WORKDIR = Path(os.environ["SHAPR3D_MCP_WORKDIR"])

BOX_SCRIPT = "result = Box(10, 10, 10)"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_safe_name_sanitizes():
    assert server._safe_name("brkt/..\\evil:name") == "brkt_.._evil_name"
    assert server._safe_name("   ") == "model"
    assert server._safe_name("bracket v1.2") == "bracket v1.2"


def test_resolve_relative_against_workdir():
    assert server._resolve("foo.step") == WORKDIR / "foo.step"
    assert server._resolve("/tmp/abs.step") == Path("/tmp/abs.step")


# ---------------------------------------------------------------------------
# _run_script contract
# ---------------------------------------------------------------------------

def test_run_script_missing_result_raises():
    with pytest.raises(RuntimeError, match="`result`"):
        server._run_script("x = Box(1, 1, 1)")


def test_run_script_error_includes_traceback():
    with pytest.raises(RuntimeError, match="boom-test-marker"):
        server._run_script("raise ValueError('boom-test-marker')")


def test_run_script_unwraps_all_builders():
    part = server._run_script(
        "with BuildPart() as p:\n    Box(5, 5, 5)\nresult = p"
    )
    assert hasattr(part, "wrapped") and part.volume == pytest.approx(125)

    sketch = server._run_script(
        "with BuildSketch() as sk:\n    Rectangle(4, 3)\nresult = sk"
    )
    assert hasattr(sketch, "wrapped") and sketch.area == pytest.approx(12)

    line = server._run_script(
        "with BuildLine() as ln:\n    Line((0, 0), (3, 4))\nresult = ln"
    )
    assert hasattr(line, "wrapped")
    assert sum(e.length for e in line.edges()) == pytest.approx(5)


def test_run_script_non_shape_result_raises():
    with pytest.raises(RuntimeError, match="not an exportable shape"):
        server._run_script("result = 42")


def test_run_script_empty_shape_raises_clearly():
    # Part() has wrapped=None; must hit the clear error, not a pybind crash.
    with pytest.raises(RuntimeError, match="not an exportable shape"):
        server._run_script("result = Part()")


def test_buildsketch_on_workplane_keeps_orientation():
    sketch = server._run_script(
        "with BuildSketch(Plane.XZ) as s:\n    Circle(5)\nresult = s"
    )
    size = sketch.bounding_box().size
    assert (size.X, size.Y, size.Z) == pytest.approx((10, 0, 10), abs=1e-6)


# ---------------------------------------------------------------------------
# export behavior
# ---------------------------------------------------------------------------

def test_dotted_names_do_not_collide():
    a = server.create_model("dot v1.2", BOX_SCRIPT)
    b = server.create_model("dot v1.3", "result = Box(20, 20, 20)")
    assert a["files"] == [str(WORKDIR / "dot v1.2.step")]
    assert b["files"] == [str(WORKDIR / "dot v1.3.step")]
    assert all(Path(f).exists() for f in a["files"] + b["files"])


def test_failed_export_raises_instead_of_reporting_phantom_files(tmp_path):
    from build123d import Edge

    edge = Edge.make_line((0, 0, 0), (1, 0, 0))  # no faces: STL export fails
    with pytest.raises(RuntimeError, match="failed"):
        server._export(edge, tmp_path / "phantom", ["stl"])
    assert not (tmp_path / "phantom.stl").exists()


def test_gltf_reports_bin_sidecar():
    out = server.create_model("sidecar", BOX_SCRIPT, formats=["gltf"])
    names = [Path(f).name for f in out["files"]]
    assert names == ["sidecar.gltf", "sidecar.bin"]
    assert all(Path(f).exists() for f in out["files"])


def test_glb_is_single_self_contained_file():
    out = server.create_model("solo", BOX_SCRIPT, formats=["glb"])
    names = [Path(f).name for f in out["files"]]
    assert names == ["solo.glb"]
    assert Path(out["files"][0]).exists()


# ---------------------------------------------------------------------------
# import / inspect / modify / convert
# ---------------------------------------------------------------------------

def test_create_then_inspect_roundtrip():
    created = server.create_model("rt_box", BOX_SCRIPT)
    info = server.inspect_model("rt_box.step")
    assert info["solids"] == 1
    assert info["volume_mm3"] == pytest.approx(1000, rel=1e-3)
    assert info["bounding_box_mm"]["size"] == [10, 10, 10]
    assert created["stats"]["volume_mm3"] == info["volume_mm3"]


def test_modify_model_drills_hole():
    server.create_model("drill_me", BOX_SCRIPT)
    out = server.modify_model(
        "drill_me.step",
        "result = imported - Pos(0, 0, 0) * Cylinder(2, 50)",
    )
    # 10^3 minus a pi*r^2*h=10 cylinder core
    assert out["stats"]["volume_mm3"] == pytest.approx(1000 - 125.664, rel=1e-3)


def test_iges_import():
    from build123d import Box
    from OCP.IGESControl import IGESControl_Writer

    iges = WORKDIR / "cube.iges"
    writer = IGESControl_Writer()
    writer.AddShape(Box(10, 10, 10).wrapped)
    assert writer.Write(str(iges))

    info = server.inspect_model("cube.iges")
    assert info["faces"] >= 6
    assert info["bounding_box_mm"]["size"] == [10, 10, 10]


def test_iges_import_keeps_stdout_clean():
    # OCCT's IGES reader prints transfer chatter to fd 1 unless silenced,
    # which would corrupt the MCP stdio transport. Must check in a FRESH
    # process: any prior build123d export masks the leak in-process.
    import subprocess
    import sys

    iges = WORKDIR / "stdout_probe.iges"
    if not iges.exists():
        from build123d import Box
        from OCP.IGESControl import IGESControl_Writer

        writer = IGESControl_Writer()
        writer.AddShape(Box(10, 10, 10).wrapped)
        assert writer.Write(str(iges))

    code = (
        "from pathlib import Path\n"
        "from shapr3d_mcp import server\n"
        f"server._import_any(Path({str(iges)!r}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        env={**os.environ, "SHAPR3D_MCP_WORKDIR": str(WORKDIR)},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout == b"", f"stdout not clean: {proc.stdout!r}"


def test_3mf_import_keeps_all_bodies():
    server.create_model(
        "two_bodies",
        "result = Compound(children=[Box(5, 5, 5), Pos(20, 0, 0) * Box(5, 5, 5)])",
        formats=["3mf"],
    )
    info = server.inspect_model("two_bodies.3mf")
    assert info["solids"] == 2


def test_convert_model_formats():
    server.create_model("conv", BOX_SCRIPT)
    out = server.convert_model("conv.step", ["stl", "brep", "3mf"])
    assert {Path(f).suffix for f in out["files"]} == {".stl", ".brep", ".3mf"}
    assert all(Path(f).exists() for f in out["files"])


def test_render_preview_writes_png():
    server.create_model("prev", BOX_SCRIPT)
    img = server.render_preview("prev.step")
    png = WORKDIR / "prev_preview.png"
    assert png.exists() and png.stat().st_size > 1000
    assert img.data or img.path  # FastMCP Image payload populated


# ---------------------------------------------------------------------------
# list_models robustness
# ---------------------------------------------------------------------------

def test_list_models_skips_broken_symlinks_and_dirs():
    real = WORKDIR / "listed.step"
    server.create_model("listed", BOX_SCRIPT)
    (WORKDIR / "ghost.step").symlink_to(WORKDIR / "does_not_exist.step")
    (WORKDIR / "folder.step").mkdir(exist_ok=True)
    try:
        paths = [m["path"] for m in server.list_models()]
        assert str(real) in paths
        assert str(WORKDIR / "ghost.step") not in paths
        assert str(WORKDIR / "folder.step") not in paths
    finally:
        (WORKDIR / "ghost.step").unlink()
        (WORKDIR / "folder.step").rmdir()
