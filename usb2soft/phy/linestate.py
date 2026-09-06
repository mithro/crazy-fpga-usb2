"""UTMI ``line_state`` for the soft PHY (spec §4.5).

``HSLineState``: the squelch-derived line state a real high-speed UTMI PHY reports — SE0 (0b00)
while the receiver sees no activity, J (0b01) / K (0b10) from the differential level while a
packet is on the wire. LUNA uses it to time bus reset (3 ms of SE0 in HS) and to keep its idle
timers cleared while traffic flows.

``LineStateSynthesiser``: for platforms without full-speed levels (HDMI pairs, internal wires) it
plays the *host's* half of reset-and-chirp towards LUNA: SE0 from power-up (LUNA needs > 5 µs of
SE0 to start high-speed detection) and, every time a device chirp ends (``op_mode == 2`` with
``tx_valid`` held for at least ``min_chirp_cycles`` and then falling), three K/J pairs, each level
held well over LUNA's 2.5 µs minimum and all
done well inside its 2.5 ms window. Outside those windows the squelch-derived state passes
through. Re-arming on every device chirp is what lets LUNA recover after a 3 ms idle gap or a
peer reset (it drops to FS, sees SE0, and chirps again).
"""
from amaranth import Elaboratable, Module, Signal, Mux, Cat
from amaranth.lib.cdc import FFSynchronizer

__all__ = ["HSLineState", "LineStateSynthesiser", "SE0", "J", "K", "SE1"]

SE0, J, K, SE1 = 0b00, 0b01, 0b10, 0b11
OP_MODE_CHIRP = 2


class HSLineState(Elaboratable):
    def __init__(self, *, cdr_domain="rx_cdr", usb_domain="usb"):
        self.cdr_domain = cdr_domain
        self.usb_domain = usb_domain
        self.activity = Signal()      # cdr domain: CDR sees edges (digital squelch open)
        self.level = Signal()         # cdr domain: latest sample level (1 = J)
        self.line_state = Signal(2)   # usb domain
        self.squelch = Signal()       # usb domain: ~activity

    def elaborate(self, platform):
        m = Module()
        # activity and level are registered together in the cdr domain and cross as a pair; the
        # two bits may still skew by one usb cycle (a 1-cycle J/K glitch), which is harmless: LUNA
        # only tests HS line_state against SE0, and K/J timing comes from the synthesiser.
        pair = Signal(2)
        m.d[self.cdr_domain] += pair.eq(Cat(self.level, self.activity))
        synced = Signal(2)
        m.submodules.sync = FFSynchronizer(pair, synced, o_domain=self.usb_domain)
        act, lvl = synced[1], synced[0]
        m.d.comb += [
            self.line_state.eq(Mux(act, Mux(lvl, J, K), SE0)),
            self.squelch.eq(~act),
        ]
        return m


class LineStateSynthesiser(Elaboratable):
    def __init__(self, *, se0_cycles=600, gap_cycles=60, hold_cycles=300, pairs=3,
                 min_chirp_cycles=60_000, domain="usb"):
        """Defaults at 60 MHz: 10 µs of SE0 at power-up, 1 µs gap after the device chirp, 5 µs
        per K/J level (LUNA needs ≥ 2.5 µs each and all pairs within 2.5 ms). Only a K burst of
        at least ``min_chirp_cycles`` (1 ms; the device chirp is 2 ms) counts as a chirp: LUNA
        still answers packets while ``op_mode`` is 2 (from START_HS_DETECTION to IS_HIGH_SPEED),
        and those short ``tx_valid`` pulses must not restart the replay."""
        self.se0_cycles, self.gap_cycles, self.hold_cycles, self.pairs = se0_cycles, gap_cycles, hold_cycles, pairs
        self.min_chirp_cycles = min_chirp_cycles
        self.domain = domain
        self.op_mode = Signal(2)
        self.tx_valid = Signal()
        self.squelch_line_state = Signal(2)
        self.line_state = Signal(2)
        self.active = Signal()        # synthesiser currently owns line_state
        self.chirps = Signal(16)      # device chirps seen (statistics)

    def elaborate(self, platform):
        m = Module()

        chirping = (self.op_mode == OP_MODE_CHIRP) & self.tx_valid
        prev = Signal()
        m.d[self.domain] += prev.eq(chirping)
        # Length filter: a genuine device chirp is a long K burst; short pulses are packets.
        length = Signal(range(self.min_chirp_cycles + 1))
        with m.If(~chirping):
            m.d[self.domain] += length.eq(0)
        with m.Elif(length != self.min_chirp_cycles):
            m.d[self.domain] += length.eq(length + 1)
        chirp_end = prev & ~chirping & (length == self.min_chirp_cycles)
        with m.If(chirp_end):
            m.d[self.domain] += self.chirps.eq(self.chirps + 1)

        timer = Signal(range(max(self.se0_cycles, self.gap_cycles, self.hold_cycles) + 1))
        pair = Signal(range(self.pairs + 1))
        m.d.comb += self.line_state.eq(self.squelch_line_state)

        def hold(cycles, then):
            with m.If(timer == cycles - 1):
                m.d[self.domain] += timer.eq(0)
                then()
            with m.Else():
                m.d[self.domain] += timer.eq(timer + 1)
            rearm()

        def rearm():
            # A device chirp ending in any state (re)starts the host replay; this comes last in
            # each state so it overrides the hold transition.
            with m.If(chirp_end):
                m.d[self.domain] += timer.eq(0)
                m.next = "GAP"

        with m.FSM(domain=self.domain) as fsm:
            m.d.comb += self.active.eq(~fsm.ongoing("PASS"))
            with m.State("SE0"):
                m.d.comb += self.line_state.eq(SE0)
                def _to_pass():
                    m.next = "PASS"
                hold(self.se0_cycles, _to_pass)
            with m.State("PASS"):
                rearm()
            with m.State("GAP"):
                def _to_k():
                    m.d[self.domain] += pair.eq(0)
                    m.next = "K"
                hold(self.gap_cycles, _to_k)
            with m.State("K"):
                m.d.comb += self.line_state.eq(K)
                def _to_j():
                    m.next = "J"
                hold(self.hold_cycles, _to_j)
            with m.State("J"):
                m.d.comb += self.line_state.eq(J)
                def _next_pair():
                    m.d[self.domain] += pair.eq(pair + 1)
                    with m.If(pair == self.pairs - 1):
                        m.next = "PASS"
                    with m.Else():
                        m.next = "K"
                hold(self.hold_cycles, _next_pair)
        return m
