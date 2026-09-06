from amaranth.sim import Simulator
from usb2soft.io.uart import UARTReceiver

DIV = 8


def _uart_bits(byte, stop_ok=True):
    return [0] + [(byte >> i) & 1 for i in range(8)] + [1 if stop_ok else 0]


def _send(dut, bytes_, gap=0, stop_ok=True):
    async def drive(ctx):
        ctx.set(dut.rx, 1)
        for _ in range(3 * DIV):
            await ctx.tick()
        for b in bytes_:
            for bit in _uart_bits(b, stop_ok):
                ctx.set(dut.rx, bit)
                for _ in range(DIV):
                    await ctx.tick()
            ctx.set(dut.rx, 1)
            for _ in range(gap):
                await ctx.tick()
        for _ in range(4 * DIV):
            await ctx.tick()
    return drive


def test_receives_back_to_back_bytes():
    dut = UARTReceiver(divisor=DIV)
    payload = [0x55, 0xA5, 0x00, 0xFF, 0x3C]
    received, errors = [], []

    async def collect(ctx):
        for _ in range(len(payload) * 10 * DIV + 10 * DIV):
            if ctx.get(dut.valid):
                received.append(ctx.get(dut.data))
            errors.append(ctx.get(dut.error))
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1e-6)
    sim.add_testbench(_send(dut, payload))
    sim.add_testbench(collect)
    sim.run()
    assert received == payload
    assert not any(errors)


def test_framing_error_on_bad_stop_bit():
    dut = UARTReceiver(divisor=DIV)
    received, errors = [], 0

    async def collect(ctx):
        nonlocal errors
        for _ in range(20 * DIV):
            if ctx.get(dut.valid):
                received.append(ctx.get(dut.data))
            errors += ctx.get(dut.error)
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1e-6)
    sim.add_testbench(_send(dut, [0x81], stop_ok=False))
    sim.add_testbench(collect)
    sim.run()
    assert received == []
    assert errors == 1


def test_glitch_shorter_than_half_bit_is_ignored():
    dut = UARTReceiver(divisor=DIV)
    received = []

    async def drive(ctx):
        ctx.set(dut.rx, 1)
        for _ in range(2 * DIV):
            await ctx.tick()
        ctx.set(dut.rx, 0)          # 2-cycle glitch, far shorter than DIV/2
        await ctx.tick()
        await ctx.tick()
        ctx.set(dut.rx, 1)
        for _ in range(12 * DIV):
            await ctx.tick()

    async def collect(ctx):
        for _ in range(16 * DIV):
            if ctx.get(dut.valid):
                received.append(ctx.get(dut.data))
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1e-6)
    sim.add_testbench(drive)
    sim.add_testbench(collect)
    sim.run()
    assert received == []
