from amaranth import Signal, Module, Elaboratable
from amaranth.sim import Simulator
from usb2soft.debug.report import TextReporter, Hex


class _Wrap(Elaboratable):
    def __init__(self):
        self.value = Signal(12)
        self.flag = Signal(1)
        self.rep = TextReporter([b"V=", Hex(self.value), b" F=", Hex(self.flag), b"\r\n"])

    def elaborate(self, platform):
        m = Module()
        m.submodules.rep = self.rep
        return m


def _collect(dut, trigger_twice=False):
    out = bytearray()

    async def tb(ctx):
        ctx.set(dut.value, 0xABC)
        ctx.set(dut.flag, 1)
        ctx.set(dut.rep.stream.ready, 1)
        ctx.set(dut.rep.trigger, 1)
        await ctx.tick()
        # Optionally keep the trigger asserted for one more cycle while the reporter is already
        # busy: it must be ignored. Sample every cycle (including that one) so no byte is missed.
        ctx.set(dut.rep.trigger, 1 if trigger_twice else 0)
        for _ in range(60):
            if ctx.get(dut.rep.stream.valid):
                out.append(ctx.get(dut.rep.stream.payload))
            await ctx.tick()
            ctx.set(dut.rep.trigger, 0)

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6)
    sim.add_testbench(tb)
    sim.run()
    return bytes(out)


def test_renders_literals_and_hex():
    assert _collect(_Wrap()) == b"V=ABC F=1\r\n"


def test_retrigger_while_busy_is_ignored():
    assert _collect(_Wrap(), trigger_twice=True) == b"V=ABC F=1\r\n"


def test_backpressure_holds_payload():
    dut = _Wrap()
    out = bytearray()

    async def tb(ctx):
        ctx.set(dut.value, 0x123)
        ctx.set(dut.rep.trigger, 1)
        await ctx.tick()
        ctx.set(dut.rep.trigger, 0)
        for cycle in range(200):
            ready = (cycle % 3) == 0
            ctx.set(dut.rep.stream.ready, ready)
            if ctx.get(dut.rep.stream.valid) and ready:
                out.append(ctx.get(dut.rep.stream.payload))
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6)
    sim.add_testbench(tb)
    sim.run()
    assert bytes(out) == b"V=123 F=0\r\n"
