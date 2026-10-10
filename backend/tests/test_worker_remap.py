

def test_remap_pins_start_tool_when_no_initial_t(tmp_path):
    # Orca omits the T-command for the starting tool (assumes T0 default).
    # The remap must insert an explicit T for the mapped start tool.
    from slicer.worker import remap_gcode_tools
    g = tmp_path / "a.gcode"
    g.write_text("G28\nG1 X10 Y10 E5\nT1\nG1 X20 E5\n", encoding="utf-8")
    # slot 0 -> physical T2, slot 1 -> physical T0
    assert remap_gcode_tools(g, {0: 2, 1: 0}) is True
    lines = g.read_text(encoding="utf-8").splitlines()
    # First extrusion (line 2) should be preceded by T2 pin.
    assert lines[1] == "T2 ; bridge: pin start tool"
    assert lines[2] == "G1 X10 Y10 E5"
    assert lines[3] == "T0"  # remapped from T1


def test_remap_no_pin_when_initial_t_present(tmp_path):
    from slicer.worker import remap_gcode_tools
    g = tmp_path / "b.gcode"
    g.write_text("T0\nG1 X10 E5\nT1\nG1 X20 E5\n", encoding="utf-8")
    assert remap_gcode_tools(g, {0: 2, 1: 0}) is True
    lines = g.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "T2"  # remapped, no extra pin inserted
    assert "pin start tool" not in g.read_text(encoding="utf-8")
