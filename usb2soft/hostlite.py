"""``HostLite``: a script-driven USB high-speed host for hardware self-tests.

It is not a USB host controller: it plays a fixed list of constant packets (built in Python at
elaboration with ``usb2soft.sim.usb_crc``) towards a ``SoftUTMIPHY`` and compares the device's
replies byte-for-byte. The default script enumerates a LUNA device (GET_DESCRIPTOR at address 0,
SET_ADDRESS 5, then a steady-state GET_DESCRIPTOR loop at address 5) and keeps SOFs flowing at
transaction boundaries so the device never sees 3 ms of squelch.

Robustness rules (from the P5 plan and code reviews): a power-up hold-off before the script
starts; after ``max_faults`` consecutive timeouts/bad replies the host goes *silent* for
``restart_delay`` (longer than the device's 3 ms squelch → FS → re-chirp path, so the device
resets to address 0) and restarts the script from step 0; stray packets are ignored while no
reply is awaited; NAK retries the transaction. Same-transmitter gaps (token → DATA) are 16 usb
cycles = 128 bit times (USB 2.0 §7.1.18.2 asks for ≥ 88).
"""
from amaranth import Elaboratable, Module, Signal, Array, Const, Cat, Mux

from .sim.usb_crc import PID, token, data, handshake, crc5
from .sim.host import setup_bytes, GET_DESCRIPTOR_DEVICE, SET_ADDRESS

__all__ = ["HostLite", "Step", "enumeration_script"]

EXPECT_NONE, EXPECT_ACK, EXPECT_DATA = 0, 1, 2


class Step:
    def __init__(self, tx, expect=EXPECT_NONE, reply=b"", *, txn_start=False, loop_target=None,
                 gap_cycles=None):
        self.tx = bytes(tx)
        self.expect = expect
        self.reply = bytes(reply)          # exact expected bytes for EXPECT_DATA
        self.txn_start = txn_start         # an SOF may be inserted before this step
        self.loop_target = loop_target     # after this step, jump here (steady-state loop)
        # same-transmitter gap (>= 88 bit times = 11 usb cycles) after a packet with no reply
        self.gap_cycles = gap_cycles if gap_cycles is not None else (16 if expect == EXPECT_NONE else 4)


def enumeration_script(device_descriptor, *, address=5):
    """Steps for: GET_DESCRIPTOR(64)@0, SET_ADDRESS, then loop GET_DESCRIPTOR(18)@address."""
    d = bytes(device_descriptor)
    ack = handshake(PID.ACK)

    def get_descriptor(addr, length):
        return [
            Step(token(PID.SETUP, addr, 0), txn_start=True),
            Step(data(PID.DATA0, GET_DESCRIPTOR_DEVICE(length)), EXPECT_ACK),
            Step(token(PID.IN, addr, 0), EXPECT_DATA, data(PID.DATA1, d), txn_start=True),
            Step(ack),
            Step(token(PID.OUT, addr, 0), txn_start=True),
            Step(data(PID.DATA1, b""), EXPECT_ACK),
        ]

    steps = get_descriptor(0, 64)
    steps += [
        Step(token(PID.SETUP, 0, 0), txn_start=True),
        Step(data(PID.DATA0, SET_ADDRESS(address)), EXPECT_ACK),
        Step(token(PID.IN, 0, 0), EXPECT_DATA, data(PID.DATA1, b""), txn_start=True),
        Step(ack),
    ]
    loop_start = len(steps)
    steps += get_descriptor(address, 18)
    steps[-1].loop_target = loop_start
    return steps


def _crc5_terms():
    """CRC5 of an 11-bit field as XOR of per-bit constants (linearity): crc = C0 ^ xor(bit_i ? T_i)."""
    c0 = crc5(0)
    return c0, [crc5(1 << i) ^ c0 for i in range(11)]


class HostLite(Elaboratable):
    def __init__(self, steps, *, sof_period=7500, start_delay=180_000, restart_delay=360_000,
                 reply_timeout=256, max_faults=4, domain="usb"):
        self.steps = list(steps)
        self.sof_period = sof_period
        self.start_delay = start_delay
        # 6 ms of silence: the device sees > 3 ms of squelch, drops to FS, re-chirps (2 ms) and
        # is back at address 0 when the script restarts.
        self.restart_delay = restart_delay
        self.reply_timeout = reply_timeout
        self.max_faults = max_faults
        self.domain = domain
        # UTMI towards the host PHY
        self.tx_data = Signal(8)
        self.tx_valid = Signal()
        self.tx_ready = Signal()
        self.rx_data = Signal(8)
        self.rx_valid = Signal()
        self.rx_active = Signal()
        self.rx_error = Signal()
        # statistics
        self.loops = Signal(32)        # steady-state loops completed
        self.ok = Signal(32)           # replies matched
        self.bad = Signal(32)          # replies that did not match
        self.timeouts = Signal(32)
        self.naks = Signal(32)
        self.restarts = Signal(32)
        self.sofs = Signal(32)
        self.step = Signal(range(len(self.steps) + 1))
        self.running = Signal()

    def elaborate(self, platform):
        m = Module()

        steps = self.steps
        n_steps = len(steps)

        # --- constant tables ---------------------------------------------------------------
        tx_bytes, tx_start, tx_len = [], [], []
        rp_bytes, rp_start, rp_len = [], [], []
        for s in steps:
            tx_start.append(len(tx_bytes)); tx_len.append(len(s.tx)); tx_bytes += list(s.tx)
            rp_start.append(len(rp_bytes)); rp_len.append(len(s.reply)); rp_bytes += list(s.reply)
        tx_rom = Array(Const(b, 8) for b in tx_bytes)
        rp_rom = Array(Const(b, 8) for b in (rp_bytes or [0]))
        arr = lambda vals, w: Array(Const(v, w) for v in vals)
        step = self.step
        cur_tx_start = arr(tx_start, range(len(tx_bytes) + 1))[step]
        cur_tx_len = arr(tx_len, 6)[step]
        cur_rp_start = arr(rp_start, range(len(rp_bytes) + 2))[step]
        cur_rp_len = arr(rp_len, 6)[step]
        cur_expect = arr([s.expect for s in steps], 2)[step]
        cur_txn = arr([int(s.txn_start) for s in steps], 1)[step]
        cur_gap = arr([s.gap_cycles for s in steps], 6)[step]
        cur_next = arr([s.loop_target if s.loop_target is not None else i + 1 for i, s in enumerate(steps)],
                       range(n_steps + 1))[step]
        cur_loops = arr([int(s.loop_target is not None) for s in steps], 1)[step]

        # --- SOF packet: A5, frame[7:0], frame[10:8] | crc5 << 3 ------------------------------
        frame = Signal(11)
        microframe = Signal(3)
        c0, terms = _crc5_terms()
        crc = Const(c0, 5)
        for i, t in enumerate(terms):
            crc = crc ^ (frame[i].replicate(5) & Const(t, 5))
        sof_bytes = Array([Const(PID.byte(PID.SOF), 8), frame[0:8], Cat(frame[8:11], crc)])
        sof_due = Signal()
        sof_timer = Signal(range(self.sof_period))
        with m.If(sof_timer == self.sof_period - 1):
            m.d[self.domain] += [sof_timer.eq(0), sof_due.eq(1)]
        with m.Else():
            m.d[self.domain] += sof_timer.eq(sof_timer + 1)

        # --- transmit / receive bookkeeping ----------------------------------------------------
        idx = Signal(7)                  # byte index in the packet being sent / compared
        timer = Signal(range(max(self.start_delay, self.restart_delay, self.reply_timeout, 64) + 1))
        hold_target = Signal.like(timer, init=self.start_delay - 1)
        gap_target = Signal(6, init=16)     # latched per step in ADVANCE (cur_gap changes with step)
        faults = Signal(range(self.max_faults + 1))
        mismatch = Signal()
        sending_sof = Signal()
        rx_prev_active = Signal()
        m.d[self.domain] += rx_prev_active.eq(self.rx_active)
        rx_end = rx_prev_active & ~self.rx_active
        nak_seen = Signal()

        def fault(counter):
            m.d[self.domain] += [counter.eq(counter + 1), timer.eq(0), gap_target.eq(16)]
            with m.If(faults == self.max_faults - 1):
                m.d[self.domain] += [faults.eq(0), self.restarts.eq(self.restarts + 1), step.eq(0),
                         hold_target.eq(self.restart_delay - 1)]
                m.next = "HOLD"
            with m.Else():
                # retry the whole transaction (token + data), like a NAK
                m.d[self.domain] += faults.eq(faults + 1)
                with m.If(~cur_txn):
                    m.d[self.domain] += step.eq(step - 1)
                m.next = "GAP"

        with m.FSM(domain=self.domain) as fsm:
            m.d.comb += self.running.eq(~fsm.ongoing("HOLD"))
            with m.State("HOLD"):
                # power-up hold-off (also used after a restart, shorter)
                with m.If(timer == hold_target):
                    m.d[self.domain] += [timer.eq(0), idx.eq(0)]
                    m.next = "DISPATCH"
                with m.Else():
                    m.d[self.domain] += timer.eq(timer + 1)
            with m.State("DISPATCH"):
                m.d[self.domain] += [idx.eq(0), mismatch.eq(0), nak_seen.eq(0), timer.eq(0)]
                with m.If(sof_due & cur_txn):
                    m.d[self.domain] += [sof_due.eq(0), sending_sof.eq(1)]
                    m.next = "SEND"
                with m.Else():
                    m.d[self.domain] += sending_sof.eq(0)
                    m.next = "SEND"
            with m.State("SEND"):
                length = Signal(6)
                m.d.comb += [
                    length.eq(cur_tx_len),
                    self.tx_valid.eq(1),
                    self.tx_data.eq(tx_rom[cur_tx_start + idx]),
                ]
                with m.If(sending_sof):
                    m.d.comb += [length.eq(3), self.tx_data.eq(sof_bytes[idx[:2]])]
                with m.If(self.tx_ready):
                    m.d[self.domain] += idx.eq(idx + 1)
                    with m.If(idx == length - 1):
                        m.d[self.domain] += idx.eq(0)
                        with m.If(sending_sof):
                            m.d[self.domain] += [self.sofs.eq(self.sofs + 1), microframe.eq(microframe + 1), timer.eq(0)]
                            with m.If(microframe == 7):
                                m.d[self.domain] += frame.eq(frame + 1)
                            m.next = "SOF_GAP"
                        with m.Elif(cur_expect == EXPECT_NONE):
                            m.d[self.domain] += timer.eq(0)
                            m.next = "ADVANCE"
                        with m.Else():
                            m.d[self.domain] += timer.eq(0)
                            m.next = "AWAIT"
            with m.State("SOF_GAP"):
                # same-transmitter gap after the SOF, then the real step
                with m.If(timer == 15):
                    m.next = "DISPATCH"
                with m.Else():
                    m.d[self.domain] += timer.eq(timer + 1)
            with m.State("AWAIT"):
                with m.If(self.rx_active):
                    m.d[self.domain] += [idx.eq(0), mismatch.eq(0)]
                    m.next = "RECEIVE"
                with m.Elif(timer == self.reply_timeout - 1):
                    fault(self.timeouts)
                with m.Else():
                    m.d[self.domain] += timer.eq(timer + 1)
            with m.State("RECEIVE"):
                expected = Signal(8)
                m.d.comb += expected.eq(rp_rom[cur_rp_start + idx])
                with m.If(self.rx_valid):
                    m.d[self.domain] += idx.eq(idx + 1)
                    with m.If(cur_expect == EXPECT_ACK):
                        with m.If((idx != 0) | (self.rx_data != PID.byte(PID.ACK))):
                            m.d[self.domain] += mismatch.eq(1)
                    with m.Else():
                        with m.If((idx >= cur_rp_len) | (self.rx_data != expected)):
                            m.d[self.domain] += mismatch.eq(1)
                    with m.If((idx == 0) & (self.rx_data == PID.byte(PID.NAK))):
                        m.d[self.domain] += nak_seen.eq(1)
                with m.If(self.rx_error):
                    m.d[self.domain] += mismatch.eq(1)
                with m.If(rx_end):
                    ok_len = Signal()
                    m.d.comb += ok_len.eq(Mux(cur_expect == EXPECT_ACK, idx == 1, idx == cur_rp_len))
                    with m.If(~mismatch & ok_len):
                        m.d[self.domain] += [self.ok.eq(self.ok + 1), faults.eq(0), timer.eq(0)]
                        m.next = "ADVANCE"
                    with m.Elif(nak_seen & (idx == 1)):
                        m.d[self.domain] += [self.naks.eq(self.naks + 1), timer.eq(0)]
                        m.next = "RETRY"
                    with m.Else():
                        fault(self.bad)
            with m.State("RETRY"):
                # NAK: repeat the transaction from its token step
                with m.If(timer == 63):
                    m.d[self.domain] += timer.eq(0)
                    with m.If(cur_txn):
                        m.next = "DISPATCH"
                    with m.Else():
                        m.d[self.domain] += step.eq(step - 1)
                        m.next = "DISPATCH"
                with m.Else():
                    m.d[self.domain] += timer.eq(timer + 1)
            with m.State("ADVANCE"):
                with m.If(cur_loops):
                    m.d[self.domain] += self.loops.eq(self.loops + 1)
                m.d[self.domain] += [gap_target.eq(cur_gap), step.eq(cur_next), timer.eq(0)]
                m.next = "GAP"
            with m.State("GAP"):
                with m.If(timer >= gap_target):
                    m.d[self.domain] += timer.eq(0)
                    with m.If(step == n_steps):
                        m.d[self.domain] += step.eq(0)
                    m.next = "DISPATCH"
                with m.Else():
                    m.d[self.domain] += timer.eq(timer + 1)
        return m
