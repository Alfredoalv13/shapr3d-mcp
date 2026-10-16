"""Regression tests for bugs found by exercising the live MCP tools."""

import os
from pathlib import Path

import pytest

from shapr3d_mcp import server

WORKDIR = Path(os.environ["SHAPR3D_MCP_WORKDIR"])

# 80x40x20 block with an r=8 through-hole: 64000 - pi*64*20
HOLED_SCRIPT = "result = Box(80, 40, 20) - Cylinder(8, 40)"
HOLED_VOLUME = 64000 - 4021.239


def test_stl_import_yields_solid_with_volume():
    server.create_model("stl_solid", HOLED_SCRIPT, formats=["stl"])
    info = server.inspect_model("stl_solid.stl")
    assert info["solids"] == 1
    # Tessellated, so allow a little chordal error.
    assert info["volume_mm3"] == pytest.approx(HOLED_VOLUME, rel=1e-2)


def test_convert_stl_to_step_writes_real_geometry():
    server.create_model("stl_src", HOLED_SCRIPT, formats=["stl"])
    out = server.convert_model("stl_src.stl", ["step"])
    info = server.inspect_model(out["files"][0])
    assert info["solids"] == 1
    assert info["volume_mm3"] == pytest.approx(HOLED_VOLUME, rel=1e-2)


def test_convert_default_name_never_overwrites_source():
    # Repro: stem.stl + stem.step side by side, convert stl -> step used to
    # clobber the good B-rep STEP with an empty one.
    server.create_model("clobber", HOLED_SCRIPT, formats=["step", "stl"])
    step = WORKDIR / "clobber.step"
    before = step.read_bytes()

    out = server.convert_model("clobber.stl", ["step"])

    assert step.read_bytes() == before
    assert str(step) not in out["files"]


def test_convert_same_format_does_not_overwrite_source():
    server.create_model("selfconv", HOLED_SCRIPT)
    src = WORKDIR / "selfconv.step"
    before = src.read_bytes()
    out = server.convert_model("selfconv.step", ["step"])
    assert src.read_bytes() == before
    assert str(src) not in out["files"]
    assert Path(out["files"][0]).exists()


def test_convert_explicit_output_name_equal_to_source_raises():
    server.create_model("explicit_src", HOLED_SCRIPT)
    src = WORKDIR / "explicit_src.step"
    before = src.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        server.convert_model("explicit_src.step", ["step"], output_name="explicit_src")
    assert src.read_bytes() == before


def test_modify_output_name_equal_to_source_raises():
    server.create_model("mod_src", HOLED_SCRIPT)
    src = WORKDIR / "mod_src.step"
    before = src.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        server.modify_model("mod_src.step", "result = imported", output_name="mod_src")
    assert src.read_bytes() == before


def test_step_export_of_mesh_only_face_raises(tmp_path):
    # A triangulation-only face (what import_stl returns) has no surface;
    # export_step "succeeds" but writes a file with no geometry.
    from build123d import export_stl, import_stl

    stl = tmp_path / "m.stl"
    export_stl(server._run_script(HOLED_SCRIPT), str(stl))
    mesh_face = import_stl(str(stl))
    with pytest.raises(RuntimeError, match="mesh"):
        server._export(mesh_face, tmp_path / "empty", ["step"])
    assert not (tmp_path / "empty.step").exists()


def test_render_preview_of_stl():
    server.create_model("prev_stl", HOLED_SCRIPT, formats=["stl"])
    server.render_preview("prev_stl.stl")
    png = WORKDIR / "prev_stl_preview.png"
    assert png.exists() and png.stat().st_size > 1000

