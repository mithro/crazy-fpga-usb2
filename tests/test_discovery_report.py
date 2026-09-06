import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "host"))
import discovery_report as dr  # noqa: E402

SAMPLE = """D 1C6F2A HELLO
D 1C6F2A R00 000000 00 00
D 1C6F2A R01 372A6B 12 03
D 1C6F2A R02 372A6B 13 01
garbage
D 1C6F2A R10 000000 00 00
"""


def test_parse_lines():
    t = dr.parse(SAMPLE)
    assert t.own_id == "1C6F2A"
    assert t.lanes[("0", "1")] == dr.Lane(peer="372A6B", port=1, lane=2, seen=True, inverted=True)
    assert t.lanes[("0", "2")].inverted is False
    assert t.lanes[("0", "0")].seen is False


def test_render_table():
    out = dr.render(dr.parse(SAMPLE))
    assert "rx0.d0" in out and "372A6B tx1.d1 inverted" in out
    assert "rx0.clk" in out and "-" in out
