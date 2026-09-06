from amaranth import Module, Signal, Elaboratable

from usb2soft.platforms.netv2 import NeTV2Platform
from usb2soft.clock.netv2 import NeTV2BringupClocks


class _Top(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.clocks = clocks = NeTV2BringupClocks()
        m.domains += clocks.domains          # domains are owned by the top, driven by the generator
        counter = Signal(4)
        m.d.sync += counter.eq(counter + 1)
        return m


def test_bringup_clocks_build_plan(tmp_path):
    plat = NeTV2Platform(variant="a7-35")
    plan = plat.build(_Top(), name="t", build_dir=str(tmp_path), do_build=False)
    v = plan.files["t.v"]
    assert "PLLE2_ADV" in v
    assert "IBUF" in v            # clk50 input buffer
    assert "create_clock" in plan.files["t.xdc"]


def test_domains_exist():
    from amaranth.hdl import Fragment
    frag = Fragment.get(_Top(), platform=NeTV2Platform(variant="a7-35"))
    names = set()

    def walk(f):
        names.update(f.domains.keys())
        for sub, _name, _src in f.subfragments:
            walk(sub)
    walk(frag)
    assert {"sync", "usb", "idelay_ref"} <= names
