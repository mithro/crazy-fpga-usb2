"""PHY latency budget: USB HS allows a device 192 bit times (400 ns, 24 cycles at 60 MHz) from
the last received bit to the first bit of its response. Measure the PHY's own contribution:
RX (last line bit -> rx_active low) plus TX (tx_valid -> first driven bit)."""
from amaranth import Module, Elaboratable
from amaranth.sim import Simulator

from usb2soft.rx import RxPath
from usb2soft.tx import TxPath
from usb2soft.sim import usbhs

BIT_NS = 1e9 / 480e6


class _Harness(Elaboratable):
    def __init__(self):
        self.rx = RxPath(samples_per_ui=4, samples_per_word=16, cdr_domain="cdr", usb_domain="usb")
        self.tx = TxPath(tx_domain="cdr", usb_domain="usb")

    def elaborate(self, platform):
        m = Module()
        m.submodules.rx = self.rx
        m.submodules.tx = self.tx
        return m


def measure():
    dut = _Harness()
    payload = bytes([0xE1, 0x00, 0x10])            # a token-sized packet
    line = [1] * 40 + usbhs.packet_line_bits(payload) + [1] * 400
    samples = usbhs.LineSampler(samples_per_ui=4).sample(line)
    words = usbhs.words(samples, 16)
    last_bit_sample = (40 + len(usbhs.packet_line_bits(payload))) * 4      # index of first idle sample
    t = {}

    async def feeder(ctx):
        for n, w in enumerate(words):
            ctx.set(dut.rx.samples, w)
            if n * 16 >= last_bit_sample and "rx_last_bit" not in t:
                t["rx_last_bit"] = n * (1e9 / 120e6)          # ns, cdr-domain time of the word after the packet
            await ctx.tick("cdr")

    async def mac(ctx):
        # Wait for rx_active to fall, then respond immediately (N = 0 MAC cycles).
        seen_active = False
        cycles = 0
        while True:
            a = ctx.get(dut.rx.rx_active)
            if a:
                seen_active = True
            if seen_active and not a:
                break
            await ctx.tick("usb")
            cycles += 1
        t["rx_active_low"] = cycles * (1e9 / 60e6)
        ctx.set(dut.tx.tx_valid, 1)
        ctx.set(dut.tx.tx_data, 0xD2)
        while not ctx.get(dut.tx.tx_ready):
            await ctx.tick("usb")
        await ctx.tick("usb")
        ctx.set(dut.tx.tx_valid, 0)
        for _ in range(60):
            await ctx.tick("usb")

    async def watch_tx(ctx):
        cycles = 0
        while True:
            if ctx.get(dut.tx.oe) and "tx_first_bit" not in t:
                t["tx_first_bit"] = cycles * (1e9 / 120e6)
            await ctx.tick("cdr")
            cycles += 1

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6, domain="cdr")
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(feeder)
    sim.add_testbench(mac)
    sim.add_testbench(watch_tx, background=True)
    sim.run()
    return t


def test_turnaround_within_budget():
    t = measure()
    rx_latency = t["rx_active_low"] - t["rx_last_bit"]
    tx_latency = t["tx_first_bit"] - t["rx_active_low"]
    total = t["tx_first_bit"] - t["rx_last_bit"]
    print(f"\nRX last bit -> rx_active low: {rx_latency:.0f} ns ({rx_latency/BIT_NS:.0f} bit times)")
    print(f"tx_valid (immediate) -> first driven bit: {tx_latency:.0f} ns ({tx_latency/BIT_NS:.0f} bit times)")
    print(f"PHY-only turnaround: {total:.0f} ns ({total/BIT_NS:.0f} of 192 bit times)")
    assert total < 192 * BIT_NS
    # leave the MAC at least half the budget
    assert total < 96 * BIT_NS
