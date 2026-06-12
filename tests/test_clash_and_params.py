"""Tests for detect_clash and params_file support."""

from pathlib import Path

import pytest

from shapr3d_mcp import server


def test_detect_clash_finds_overlap():
    server.create_model(
        "clash_overlap",
        "result = Compound(children=["
        "Box(20, 20, 20), Pos(15, 0, 0) * Box(20, 20, 20)])",
    )
    report = server.detect_clash(["clash_overlap.step"])
    assert report["clear"] is False
    assert report["solids"] == 2
    assert len(report["clashes"]) == 1
    # 5mm overlap in X, full 20x20 in Y/Z
    assert report["clashes"][0]["overlap_mm3"] == pytest.approx(5 * 20 * 20, rel=1e-3)


def test_detect_clash_clear_when_separated():
    server.create_model(
        "clash_apart",
        "result = Compound(children=["
        "Box(10, 10, 10), Pos(30, 0, 0) * Box(10, 10, 10)])",
    )
    report = server.detect_clash(["clash_apart.step"])
    assert report["clear"] is True
    assert report["clashes"] == []
    assert report["pairs_checked"] == 1


def test_detect_clash_across_files():
    server.create_model("clash_a", "result = Box(10, 10, 10)")
    server.create_model("clash_b", "result = Pos(2, 0, 0) * Box(10, 10, 10)")
    report = server.detect_clash(["clash_a.step", "clash_b.step"])
    assert report["clear"] is False
    assert report["clashes"][0]["overlap_mm3"] == pytest.approx(8 * 10 * 10, rel=1e-3)


def test_detect_clash_touching_faces_not_a_clash():
    # Coincident faces share zero volume - must not be reported.
    server.create_model(
        "clash_touch",
        "result = Compound(children=["
        "Box(10, 10, 10), Pos(10, 0, 0) * Box(10, 10, 10)])",
    )
    report = server.detect_clash(["clash_touch.step"])
    assert report["clear"] is True


def test_detect_clash_needs_two_solids():
    server.create_model("clash_single", "result = Box(5, 5, 5)")
    with pytest.raises(ValueError, match="at least 2"):
        server.detect_clash(["clash_single.step"])


def test_params_file_yaml(tmp_path):
    params = tmp_path / "params.yaml"
    params.write_text("box:\n  length: 42.0\n  width: 7.0\n  height: 3.0\n")
    out = server.create_model(
        "params_box",
        "b = params['box']\n"
        "result = Box(b['length'], b['width'], b['height'])",
        params_file=str(params),
    )
    assert out["stats"]["bounding_box_mm"]["size"] == [42.0, 7.0, 3.0]


def test_params_file_json(tmp_path):
    params = tmp_path / "params.json"
    params.write_text('{"dia": 10.0, "h": 4.0}')
    out = server.create_model(
        "params_cyl",
        "result = Cylinder(params['dia'] / 2, params['h'])",
        params_file=str(params),
    )
    assert out["stats"]["bounding_box_mm"]["size"] == [10.0, 10.0, 4.0]


def test_params_file_missing():
    with pytest.raises(FileNotFoundError):
        server.create_model(
            "params_missing", "result = Box(1, 1, 1)",
            params_file="does_not_exist.yaml",
        )


def test_params_file_in_modify_model(tmp_path):
    params = tmp_path / "p.yaml"
    params.write_text("hole_dia: 6.0\n")
    server.create_model("params_base", "result = Box(30, 30, 10)")
    out = server.modify_model(
        "params_base.step",
        "result = imported - Cylinder(params['hole_dia'] / 2, 100)",
        output_name="params_base_drilled",
        params_file=str(params),
    )
    base_vol = 30 * 30 * 10
    hole_vol = 3.1415926 * 3**2 * 10
    assert out["stats"]["volume_mm3"] == pytest.approx(base_vol - hole_vol, rel=1e-3)
