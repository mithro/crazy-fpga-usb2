from amaranth.sim import Simulator
from usb2soft.applets.hello import HelloCore

DIV = 4


def test_banner_contains_identity():
    dut = HelloCore(divisor=DIV, period_cycles=50, variant="a7-35", version="v0.0-test",
                    simulate_dna=0x0123456789ABCDE)
    line = bytearray()

    async def uart_rx(ctx):
        # Sample the UART TX line at mid-bit.
        while len(line) < 80:
            while ctx.get(dut.uart_tx):
                await ctx.tick()
            for _ in range(DIV + DIV // 2):
                await ctx.tick()
            byte = 0
            for i in range(8):
                byte |= ctx.get(dut.uart_tx) << i
                for _ in range(DIV):
                    await ctx.tick()
            line.append(byte)
            if line.endswith(b"\r\n"):
                break

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6)
    sim.add_testbench(uart_rx)
    sim.run()
    text = bytes(line).decode()
    assert text == "usb2soft hello a7-35 v0.0-test DNA=0123456789ABCDE\r\n"


def test_heartbeat_toggles():
    dut = HelloCore(divisor=DIV, period_cycles=20, variant="a7-35", version="v", simulate_dna=1)
    seen = set()

    async def tb(ctx):
        for _ in range(100):
            seen.add(ctx.get(dut.leds[0]))
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6)
    sim.add_testbench(tb)
    sim.run()
    assert seen == {0, 1}
