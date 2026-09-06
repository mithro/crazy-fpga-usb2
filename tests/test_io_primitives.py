import re
from amaranth import Module, Signal, Elaboratable
from amaranth.back import rtlil

from usb2soft.platforms.netv2 import NeTV2Platform
from usb2soft.clock.netv2 import NeTV2PhyClocks
from usb2soft.io.sampler import Sampler, IdelayCtrl, IdelayLoader
from usb2soft.io.serializer import Serializer


class _Top(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.clocks = clocks = NeTV2PhyClocks()
        m.domains += clocks.domains
        m.submodules.idc = IdelayCtrl()
        rx = platform.request("hdmi_in", 0, dir="-")
        tx = platform.request("hdmi_out", 0, dir="-")
        m.submodules.sampler = s = Sampler(rx.d0)
        m.d.comb += s.rdy.eq(m.submodules.idc.rdy)
        m.submodules.ser = ser = Serializer(tx.d0)
        acc = Signal(16)
        m.d.rx_cdr += acc.eq(acc ^ s.samples)
        m.d.tx_cdr += [ser.line.eq(acc[0:4]), ser.oe.eq(acc[4:8])]
        return m


def _verilog(tmp_path):
    plat = NeTV2Platform(variant="a7-35")
    plan = plat.build(_Top(), name="t", build_dir=str(tmp_path), do_build=False)
    return plan.files["t.v"]


def test_sampler_primitives_and_parameters(tmp_path):
    v = _verilog(tmp_path)
    assert v.count("ISERDESE2") == 2
    assert v.count("IDELAYE2") == 2
    assert v.count("IBUFDS_DIFF_OUT") == 1
    assert v.count("IDELAYCTRL") == 1
    assert re.search(r'\.INTERFACE_TYPE\("NETWORKING"\)', v)
    assert re.search(r'\.DATA_WIDTH\(32.d8\)', v)
    assert re.search(r'\.IDELAY_TYPE\("VAR_LOAD"\)', v)
    assert re.search(r'\.REFCLK_FREQUENCY\(300\.0\)', v)
    # HDMI RX0 lane 0 is an inverted pair: main path inverted, complementary OB path not.
    assert re.search(r'\.IS_IDATAIN_INVERTED\(32.d1\)', v) and re.search(r'\.IS_IDATAIN_INVERTED\(32.d0\)', v)
    assert v.count('.IS_CLKB_INVERTED(32\'d1)') == 2
    # LD must be a real signal (the tap count only changes on an LD pulse), never a constant.
    for ld in re.findall(r'\.LD\(([^)]*)\)', v):
        assert not re.fullmatch(r"\s*1'[hdb]\d+\s*", ld), ld
    assert len(re.findall(r'\.LD\(', v)) == 2


def test_serializer_primitives_and_parameters(tmp_path):
    v = _verilog(tmp_path)
    assert v.count("OSERDESE2") == 1
    assert v.count("OBUFTDS") == 1
    assert re.search(r'\.TRISTATE_WIDTH\(32.d4\)', v)
    assert re.search(r'\.DATA_RATE_TQ\("DDR"\)', v)
    assert re.search(r'\.DATA_WIDTH\(32.d4\)', v)


def test_hdmi_pins_constrained(tmp_path):
    plat = NeTV2Platform(variant="a7-35")
    plan = plat.build(_Top(), name="t", build_dir=str(tmp_path), do_build=False)
    xdc = plan.files["t.xdc"]
    for pin in ("K21", "K22", "W21", "W22"):
        assert f"LOC {pin}" in xdc


def test_idelay_loader_pulses_once_after_rdy_and_on_request():
    from amaranth.sim import Simulator
    dut = IdelayLoader("sync")
    seen = []

    async def tb(ctx):
        for _ in range(3):
            seen.append(ctx.get(dut.ld))
            await ctx.tick()
        ctx.set(dut.rdy, 1)
        for _ in range(4):
            seen.append(ctx.get(dut.ld))
            await ctx.tick()
        ctx.set(dut.load, 1)
        seen.append(ctx.get(dut.ld))
        await ctx.tick()
        ctx.set(dut.load, 0)
        for _ in range(2):
            seen.append(ctx.get(dut.ld))
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1e-8)
    sim.add_testbench(tb)
    sim.run()
    assert seen == [0, 0, 0, 1, 0, 0, 0, 1, 0, 0]
