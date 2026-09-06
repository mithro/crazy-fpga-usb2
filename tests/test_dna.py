from amaranth.sim import Simulator
from usb2soft.debug.dna import DNAReader

DNA = 0x1A2B3C4D5E6F708 & ((1 << 57) - 1)


def test_reader_reads_57_bits_msb_first():
    dut = DNAReader()

    async def model(ctx):
        # Behavioural DNA_PORT: registers READ/SHIFT on the clock edge, DOUT shows the MSB.
        shift_reg = 0
        while True:
            read = ctx.get(dut.port_read)
            shift = ctx.get(dut.port_shift)
            await ctx.tick()
            if read:
                shift_reg = DNA
            elif shift:
                shift_reg = (shift_reg << 1) & ((1 << 57) - 1)
            ctx.set(dut.port_dout, (shift_reg >> 56) & 1)

    async def check(ctx):
        for _ in range(400):
            if ctx.get(dut.valid):
                break
            await ctx.tick()
        assert ctx.get(dut.valid) == 1
        assert ctx.get(dut.dna) == DNA
        # Stays valid and stable afterwards.
        for _ in range(20):
            await ctx.tick()
        assert ctx.get(dut.valid) == 1 and ctx.get(dut.dna) == DNA

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6)
    # A background testbench: it may use ctx.get() (processes may not) and its infinite loop
    # does not keep sim.run() alive once ``check`` finishes.
    sim.add_testbench(model, background=True)
    sim.add_testbench(check)
    sim.run()
