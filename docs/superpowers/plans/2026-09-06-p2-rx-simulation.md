# P2 RX Path in Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fully simulated USB 2.0 high-speed receive path — oversampled samples in, UTMI `rx_active`/`rx_valid`/`rx_data`/`rx_error` out — proven against a Python wire model to recover packets with zero bit errors at ±2000 ppm frequency offset, with jitter, phase steps and noisy idle, for both 3x and 4x oversampling.

**Architecture:** `OversamplingCDR` (generic over S samples/UI and W samples/word) turns a 16-sample word per 120 MHz cycle into 3/4/5 recovered line bits using a phase pointer with a vote-based loop filter (spec §4.2). `PacketDecoder` consumes those small words in the same 120 MHz domain: NRZI decode, SYNC detection (≥8 zeros then a one), bit unstuffing, HS EOP detection (`01111111`), byte packing, emitting start/data/end/error events. `RxUTMIBridge` moves events through a 16-deep `AsyncFIFOBuffered` into the 60 MHz `usb` domain and produces UTMI-compliant signals. `RxPath` composes them. `usb2soft/sim/usbhs.py` is the bit-level wire model (encoding, timing, jitter, idle noise) used by every test.

**Deviation from spec §4.3, recorded here and folded back into the spec:** the decoder runs on the CDR's 5-bit words at 120 MHz instead of 10-bit words at 60 MHz after a 2:1 gearbox. Prefix logic over 5 bits is about a quarter of the size of the 10-bit version, 120 MHz is comfortable for this logic on Artix-7 -2 (and for openXC7 results), and the byte-level async FIFO that is needed anyway as the elastic buffer also removes the assumption that `rx_cdr` and `usb` have a known phase.

**Tech Stack:** Amaranth 0.5.9 (`amaranth.sim`, `amaranth.lib.fifo.AsyncFIFOBuffered`, `amaranth.lib.cdc`), pytest, NumPy-free pure Python model.

Spec: `docs/superpowers/specs/2026-09-06-usb2-soft-phy-design.md` §4.2, §4.3, §4.6, §6. Conventions and hardware rules: `CLAUDE.md`. Work on branch `p2-rx-sim` in worktree `.worktrees/p2-rx-sim` (created from `p0-bootstrap`; rebase onto `main` once PR #1 merges). Commit trailers as in earlier commits.

---

## File structure

| File | Responsibility |
|------|----------------|
| `usb2soft/sim/__init__.py` | package doc |
| `usb2soft/sim/usbhs.py` | wire model: `nrzi_encode`, `bit_stuff`, `packet_line_bits`, `LineSampler` (S, ppm, phase, jitter, idle noise, ramps), `words()` helper, `reference_decode` (Python decoder used to validate the model and, later, TX) |
| `usb2soft/rx/__init__.py` | `RxPath` composition + re-exports |
| `usb2soft/rx/cdr.py` | `OversamplingCDR` |
| `usb2soft/rx/decoder.py` | `PacketDecoder` (NRZI, SYNC, unstuff, EOP, packer → events) |
| `usb2soft/rx/bridge.py` | `RxUTMIBridge` (event FIFO rx_cdr→usb, UTMI rx signals) |
| `tests/test_usbhs_model.py`, `tests/test_cdr.py`, `tests/test_decoder.py`, `tests/test_rx_path.py` | one file per module plus the end-to-end chain |
| `docs/superpowers/specs/2026-09-06-usb2-soft-phy-design.md` | §4.3 updated for the 120 MHz decoder + byte FIFO |
| `docs/results/2026-09-XX-p2-rx-simulation.md` | measured lock/jitter/ppm margins from the test sweeps |

Conventions: bit 0 of every multi-bit "bits" signal is the earliest bit; USB bytes are LSB-first on the wire; a "line bit" is the NRZI-coded differential level (1 = J, 0 = K); "data bit" is the decoded bit. All gateware takes a `domain` constructor argument (default `"sync"`) so the same modules simulate in one domain and deploy in `rx_cdr`/`usb`.

---

### Task 1: Wire model

**Files:**
- Create: `usb2soft/sim/__init__.py`, `usb2soft/sim/usbhs.py`
- Test: `tests/test_usbhs_model.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_usbhs_model.py
import random
import pytest
from usb2soft.sim import usbhs


def test_nrzi_roundtrip():
    data = [1, 0, 0, 1, 1, 1, 0, 1]
    line = usbhs.nrzi_encode(data, initial=1)
    assert usbhs.nrzi_decode(line, initial=1) == data
    # a data 1 keeps the level, a data 0 toggles it
    assert line[0] == 1 and line[1] == 0 and line[2] == 1


def test_bit_stuffing_inserts_zero_after_six_ones():
    assert usbhs.bit_stuff([1] * 6) == [1] * 6 + [0]
    assert usbhs.bit_stuff([1] * 12) == [1] * 6 + [0] + [1] * 6 + [0]
    assert usbhs.bit_unstuff(usbhs.bit_stuff([1] * 13)) == ([1] * 13, False)


def test_packet_line_bits_structure():
    line = usbhs.packet_line_bits(bytes([0xC3, 0x00]), sync_bits=32)
    # SYNC: 31 zeros then a one -> NRZI from idle J: KJKJ...KK
    assert line[:4] == [0, 1, 0, 1]
    assert line[30:32] == [0, 0]
    # EOP: NRZ 0 then seven 1s -> one transition then a flat level for 7 bits
    eop = line[-8:]
    assert eop[0] != line[-9] and len(set(eop[1:])) == 1 and eop[1] == eop[0]
    # 32 sync + 16 data bits (no stuffing in C3 00) + 8 EOP
    assert len(line) == 32 + 16 + 8


def test_reference_decode_recovers_payload():
    payload = bytes(random.Random(1).randrange(256) for _ in range(64))
    line = usbhs.packet_line_bits(payload, sync_bits=12)
    idle = [1] * 20
    got = usbhs.reference_decode(idle + line + idle)
    assert got == [payload]


def test_sampler_static_phase_and_ppm():
    line = [1, 0] * 50
    s0 = usbhs.LineSampler(samples_per_ui=4, ppm=0, phase=0.0).sample(line)
    assert s0 == [b for b in line for _ in range(4)]
    # +500 ppm: after enough bits the receiver has taken one sample fewer
    long = [1, 0] * 2000
    s = usbhs.LineSampler(samples_per_ui=4, ppm=+500, phase=0.0).sample(long)
    assert len(s) == 4 * len(long) - 8      # 4000 UI * 500e-6 = 2 UI = 8 samples fewer


def test_sampler_jitter_and_noise_are_deterministic():
    line = usbhs.packet_line_bits(b"\x55" * 8)
    a = usbhs.LineSampler(samples_per_ui=4, rj_ui=0.05, seed=7).sample(line)
    b = usbhs.LineSampler(samples_per_ui=4, rj_ui=0.05, seed=7).sample(line)
    assert a == b and len(a) == 4 * len(line)
    noise = usbhs.idle_noise(n_ui=100, samples_per_ui=4, toggle_prob=0.3, seed=3)
    assert len(noise) == 400 and 0 < sum(noise) < 400


def test_words_groups_samples_earliest_first():
    assert usbhs.words([1, 0, 0, 1, 1, 1, 0, 0], 4) == [0b1001, 0b0011]   # bit 0 = earliest
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_usbhs_model.py -q`
Expected: FAIL, `No module named 'usb2soft.sim'`

- [ ] **Step 3: Implement the model**

```python
# usb2soft/sim/__init__.py
"""Simulation models (USB high-speed wire model, later a bit-level host)."""
```

```python
# usb2soft/sim/usbhs.py
"""Bit-level USB 2.0 high-speed wire model.

Conventions: a *data bit* is what the MAC sees; a *line bit* is the NRZI-coded differential
level on the wire, 1 = J, 0 = K. USB NRZI: a data 1 keeps the level, a data 0 toggles it. Bit
stuffing inserts a 0 after six consecutive data 1s (SYNC included). HS SYNC is 32 line bits
KJKJ...KK, i.e. data 0000...01; hubs may strip up to 20 of them. HS EOP is the NRZ byte
01111111 sent without stuffing: one transition then seven identical levels; the receiver sees
it as a bit-stuffing violation (seven ones).

``LineSampler`` produces the receiver's sample stream: ``samples_per_ui`` nominal samples per
line bit, a frequency offset in ppm (positive = transmitter faster than the sampler), an initial
phase in samples, Gaussian random jitter on every edge, a periodic sinusoidal deterministic
jitter, and an optional linear ppm ramp. Everything is deterministic given ``seed``.
"""
import math
import random

__all__ = [
    "nrzi_encode", "nrzi_decode", "bit_stuff", "bit_unstuff", "sync_data_bits", "eop_line_bits",
    "packet_line_bits", "LineSampler", "idle_noise", "words", "reference_decode",
]

HS_SYNC_BITS = 32
STUFF_RUN = 6


def nrzi_encode(data_bits, *, initial=1):
    level = initial
    out = []
    for d in data_bits:
        if not d:
            level ^= 1
        out.append(level)
    return out


def nrzi_decode(line_bits, *, initial=1):
    prev = initial
    out = []
    for lvl in line_bits:
        out.append(1 if lvl == prev else 0)
        prev = lvl
    return out


def bit_stuff(data_bits):
    out, run = [], 0
    for d in data_bits:
        out.append(d)
        run = run + 1 if d else 0
        if run == STUFF_RUN:
            out.append(0)
            run = 0
    return out


def bit_unstuff(data_bits):
    """Return (bits, violation_seen). A seventh consecutive one is a stuffing violation."""
    out, run = [], 0
    for d in data_bits:
        if run == STUFF_RUN:
            run = 0
            if d:
                return out, True
            continue            # drop the stuffed zero
        out.append(d)
        run = run + 1 if d else 0
    return out, False


def sync_data_bits(n=HS_SYNC_BITS):
    return [0] * (n - 1) + [1]


def payload_data_bits(payload):
    return [(b >> i) & 1 for b in payload for i in range(8)]


def eop_line_bits(last_level):
    # NRZ 0 then seven 1s: first level is the inverse of the last data level, then held.
    lvl = last_level ^ 1
    return [lvl] * 8


def packet_line_bits(payload, *, sync_bits=HS_SYNC_BITS, idle_level=1):
    """Line bits for one packet: SYNC + stuffed payload + EOP. No idle around it."""
    data = sync_data_bits(sync_bits) + payload_data_bits(payload)
    line = nrzi_encode(bit_stuff(data), initial=idle_level)
    return line + eop_line_bits(line[-1])


def words(samples, width):
    """Group a sample list into integers of ``width`` samples; sample 0 -> bit 0."""
    if len(samples) % width:
        samples = list(samples) + [samples[-1]] * (width - len(samples) % width)
    return [sum(samples[i + k] << k for k in range(width)) for i in range(0, len(samples), width)]


class LineSampler:
    def __init__(self, *, samples_per_ui, ppm=0.0, phase=0.0, rj_ui=0.0, dj_ui=0.0,
                 dj_period_ui=100.0, ramp_ppm_per_ui=0.0, seed=0):
        self.S = samples_per_ui
        self.ppm = ppm
        self.phase = phase
        self.rj_ui = rj_ui
        self.dj_ui = dj_ui
        self.dj_period_ui = dj_period_ui
        self.ramp = ramp_ppm_per_ui
        self.rng = random.Random(seed)

    def edge_times(self, line_bits):
        """Absolute sample-clock times (in samples) of each bit's start, including jitter."""
        times = []
        t = self.phase
        ppm = self.ppm
        for n in range(len(line_bits) + 1):
            jitter = self.rng.gauss(0.0, self.rj_ui) if self.rj_ui else 0.0
            if self.dj_ui:
                jitter += self.dj_ui * math.sin(2 * math.pi * n / self.dj_period_ui)
            times.append(t + jitter * self.S)
            # a transmitter faster by +ppm makes each UI shorter in sampler units
            t += self.S / (1.0 + ppm * 1e-6)
            ppm += self.ramp
        return times

    def sample(self, line_bits, *, tail_level=None):
        """Sample the waveform at integer sample times 0, 1, 2, ... until the last bit ends."""
        times = self.edge_times(line_bits)
        end = times[-1]
        out = []
        bit = 0
        t = 0.0
        while t < end:
            while bit + 1 < len(times) and times[bit + 1] <= t:
                bit += 1
            if t < times[0]:
                out.append(line_bits[0] if tail_level is None else tail_level)
            else:
                out.append(line_bits[min(bit, len(line_bits) - 1)])
            t += 1.0
        return out


def idle_noise(*, n_ui, samples_per_ui, toggle_prob, seed=0, start_level=1):
    """Comparator chatter on a zero-differential idle line: random toggles per sample."""
    rng = random.Random(seed)
    lvl = start_level
    out = []
    for _ in range(n_ui * samples_per_ui):
        if rng.random() < toggle_prob:
            lvl ^= 1
        out.append(lvl)
    return out


def reference_decode(line_bits, *, min_sync_zeros=8):
    """Ideal bit-rate decoder: returns the list of payloads found in a line-bit sequence."""
    data = nrzi_decode(line_bits, initial=line_bits[0])
    packets, i, zeros = [], 0, 0
    while i < len(data):
        d = data[i]
        if d == 0:
            zeros += 1
            i += 1
            continue
        if zeros < min_sync_zeros:
            zeros = 0
            i += 1
            continue
        # packet starts after this 1; consume until stuffing violation
        zeros = 0
        i += 1
        bits, run = [], 1
        while i < len(data):
            d = data[i]
            i += 1
            if run == STUFF_RUN:
                run = 0
                if d:
                    break               # EOP
                continue
            bits.append(d)
            run = run + 1 if d else 0
        # the last 7 bits (0111111) are the EOP, not payload
        bits = bits[:-7]
        payload = bytes(sum(bits[k + j] << j for j in range(8)) for k in range(0, len(bits) - len(bits) % 8, 8))
        packets.append(payload)
    return packets
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_usbhs_model.py -q`
Expected: `7 passed`. If `test_sampler_static_phase_and_ppm` is off by one sample, the culprit is the `while t < end` boundary; keep the semantics "samples strictly before the end of the last bit" and adjust the expected count in the test only if the maths says so (4000 UI × 500 ppm = 2.0 UI = exactly 8 samples).

- [ ] **Step 5: Commit**

```bash
git add usb2soft/sim tests/test_usbhs_model.py
git commit -m "sim: USB high-speed wire model (NRZI, stuffing, SYNC/EOP, jittered sampler, idle noise)"
```

---

### Task 2: Oversampling CDR

**Files:**
- Create: `usb2soft/rx/__init__.py` (docstring only for now), `usb2soft/rx/cdr.py`
- Test: `tests/test_cdr.py`

Algorithm (spec §4.2): pick phase `phi ∈ [0, S)`; picks at `phi + k·S`. Edges vote on the ideal pick; a saturating accumulator steps `phi` by ±1 (threshold 1 when not in a packet, `track_threshold` inside one); a step past `S−1` yields B−1 bits (`phi ← 0`), a step below 0 yields B+1 bits with the first taken from the previous word's last sample (`phi ← S−1`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cdr.py
import random
import pytest
from amaranth.sim import Simulator

from usb2soft.rx.cdr import OversamplingCDR
from usb2soft.sim import usbhs


def run_cdr(samples, *, S, W, in_packet_after=None, track_threshold=3):
    """Feed sample words, return the recovered line bits and the slip counts."""
    dut = OversamplingCDR(samples_per_ui=S, samples_per_word=W, track_threshold=track_threshold)
    bits, slips = [], {"up": 0, "down": 0}
    wds = usbhs.words(samples, W)

    async def tb(ctx):
        for n, w in enumerate(wds):
            ctx.set(dut.samples, w)
            if in_packet_after is not None:
                ctx.set(dut.in_packet, n >= in_packet_after)
            await ctx.tick()
            cnt = ctx.get(dut.count)
            val = ctx.get(dut.bits)
            bits.extend((val >> i) & 1 for i in range(cnt))
            slips["up"] += ctx.get(dut.slip_up)
            slips["down"] += ctx.get(dut.slip_down)
        for _ in range(3):
            await ctx.tick()
            cnt = ctx.get(dut.count)
            val = ctx.get(dut.bits)
            bits.extend((val >> i) & 1 for i in range(cnt))

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    return bits, slips


def contains(recovered, sent, *, max_skip=4):
    """True if ``sent`` appears contiguously in ``recovered`` (acquisition may eat a few bits)."""
    s = "".join(map(str, sent[max_skip:]))
    r = "".join(map(str, recovered))
    return s in r


def line_for(payload, sync=32):
    return [1] * 8 + usbhs.packet_line_bits(payload, sync_bits=sync) + [1] * 8


@pytest.mark.parametrize("S,W", [(4, 16), (3, 12)])
@pytest.mark.parametrize("phase", [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
def test_static_phase_recovers_exact_bits(S, W, phase):
    if phase >= S:
        pytest.skip("phase outside one UI")
    payload = bytes(random.Random(int(phase * 10)).randrange(256) for _ in range(32))
    line = line_for(payload)
    samples = usbhs.LineSampler(samples_per_ui=S, phase=phase).sample(line)
    bits, _ = run_cdr(samples, S=S, W=W)
    assert contains(bits, line)


@pytest.mark.parametrize("S,W", [(4, 16), (3, 12)])
@pytest.mark.parametrize("ppm", [-2000, -1000, -500, -100, 100, 500, 1000, 2000])
def test_frequency_offset_tracked_over_long_packet(S, W, ppm):
    payload = bytes(random.Random(ppm).randrange(256) for _ in range(1024))   # ~8200 line bits
    line = line_for(payload)
    samples = usbhs.LineSampler(samples_per_ui=S, ppm=ppm, phase=0.3).sample(line)
    bits, slips = run_cdr(samples, S=S, W=W, in_packet_after=3)
    assert contains(bits, line)
    expected_slips = len(line) * abs(ppm) * 1e-6 * S      # in samples
    total = slips["up"] + slips["down"]
    assert abs(total - expected_slips) <= 2
    # a faster transmitter (+ppm) means fewer samples per bit: the pick phase must move earlier
    assert (slips["down"] > slips["up"]) == (ppm > 0)


@pytest.mark.parametrize("rj_ui", [0.0, 0.05, 0.10, 0.15])
def test_random_jitter_tolerance(rj_ui):
    payload = bytes(random.Random(5).randrange(256) for _ in range(256))
    line = line_for(payload)
    samples = usbhs.LineSampler(samples_per_ui=4, ppm=300, rj_ui=rj_ui, seed=11).sample(line)
    bits, _ = run_cdr(samples, S=4, W=16, in_packet_after=3)
    assert contains(bits, line)


def test_deterministic_jitter_plus_offset():
    payload = bytes(random.Random(9).randrange(256) for _ in range(512))
    line = line_for(payload)
    samples = usbhs.LineSampler(samples_per_ui=4, ppm=-500, dj_ui=0.15, dj_period_ui=37).sample(line)
    bits, _ = run_cdr(samples, S=4, W=16, in_packet_after=3)
    assert contains(bits, line)


def test_phase_step_between_packets_reacquires():
    p1 = bytes(range(16))
    p2 = bytes(range(16, 48))
    a = usbhs.LineSampler(samples_per_ui=4, phase=0.0).sample(line_for(p1))
    b = usbhs.LineSampler(samples_per_ui=4, phase=2.0).sample(line_for(p2))
    samples = a + [1] * 40 + b
    bits, _ = run_cdr(samples, S=4, W=16)
    assert contains(bits, usbhs.packet_line_bits(p1)) and contains(bits, usbhs.packet_line_bits(p2))


def test_activity_flag_follows_edges():
    dut = OversamplingCDR(samples_per_ui=4, samples_per_word=16)
    seen = []

    async def tb(ctx):
        ctx.set(dut.samples, 0xFFFF)
        for _ in range(12):
            await ctx.tick()
        seen.append(ctx.get(dut.activity))          # flat for 48 UI -> no activity
        ctx.set(dut.samples, 0x0F0F)                 # edges every 4 samples
        await ctx.tick()
        await ctx.tick()
        seen.append(ctx.get(dut.activity))
        ctx.set(dut.samples, 0x0000)
        for _ in range(3):                           # 12 UI without an edge > idle_ui=8
            await ctx.tick()
        seen.append(ctx.get(dut.activity))

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    assert seen == [0, 1, 0]


def test_noisy_idle_then_packet_locks():
    payload = bytes(random.Random(3).randrange(256) for _ in range(64))
    noise = usbhs.idle_noise(n_ui=200, samples_per_ui=4, toggle_prob=0.25, seed=5)
    samples = noise + usbhs.LineSampler(samples_per_ui=4, ppm=200, phase=1.2).sample(line_for(payload))
    bits, _ = run_cdr(samples, S=4, W=16)
    assert contains(bits, usbhs.packet_line_bits(payload))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cdr.py -q`
Expected: FAIL, `No module named 'usb2soft.rx'`

- [ ] **Step 3: Implement the CDR**

```python
# usb2soft/rx/__init__.py
"""USB high-speed receive path: oversampling CDR, packet decoder, UTMI bridge."""
```

```python
# usb2soft/rx/cdr.py
"""Blind-oversampling clock/data recovery (spec §4.2).

Each cycle takes W samples (sample 0 earliest), S per unit interval nominally, and emits the
recovered line bits: B = W/S normally, B-1 when the pick phase wraps upward, B+1 when it wraps
downward (the extra bit being the previous word's last sample). Edges vote on where the pick
should be; a saturating accumulator turns votes into at most one +-1 phase step per cycle.
Out of a packet the threshold is 1 (fast acquisition on SYNC); inside it is ``track_threshold``.
"""
from amaranth import Elaboratable, Module, Signal, Cat, Const, Array, Mux, signed

__all__ = ["OversamplingCDR"]


def _popcount(m, bits, name):
    """Sum of a list of 1-bit expressions as a Signal."""
    total = Signal(range(len(bits) + 1), name=name)
    m.d.comb += total.eq(sum(bits))
    return total


class OversamplingCDR(Elaboratable):
    def __init__(self, *, samples_per_ui, samples_per_word, track_threshold=3, idle_ui=8,
                 domain="sync"):
        S, W = samples_per_ui, samples_per_word
        if S not in (3, 4):
            raise ValueError("samples_per_ui must be 3 or 4")
        if W % S:
            raise ValueError("samples_per_word must be a multiple of samples_per_ui")
        self.S, self.W, self.B = S, W, W // S
        self.track_threshold = track_threshold
        self.idle_ui = idle_ui
        self.domain = domain

        self.samples = Signal(W)
        self.in_packet = Signal()

        self.bits = Signal(self.B + 1)
        self.count = Signal(range(self.B + 2))
        self.activity = Signal()
        self.slip_up = Signal()
        self.slip_down = Signal()
        self.phase = Signal(range(S))

    def elaborate(self, platform):
        m = Module()
        S, W, B = self.S, self.W, self.B
        sync = m.d[self.domain]

        prev_last = Signal()
        cur = self.samples
        # ext[0] = previous word's last sample, ext[1 + i] = cur[i], zero padded.
        ext = Signal(W + S + 2)
        m.d.comb += ext.eq(Cat(prev_last, cur))
        edges = Signal(W)
        m.d.comb += edges.eq(cur ^ Cat(prev_last, cur[:-1]))

        # --- votes -------------------------------------------------------------------------
        # An edge between samples i-1 and i means the bit starting at i is best sampled at
        # i + S//2. Compare (mod S) with the current phase: +1 -> phase too early, S-1 -> too late.
        # For S=4 the equidistant case (difference 2) votes +1 so it is never a fixed point.
        up_terms, down_terms = [], []
        for i in range(W):
            ideal = (i + S // 2) % S
            up_lut = Array([Const(1 if ((ideal - phi) % S) in (1, 2) else 0, 1) for phi in range(S)])
            down_lut = Array([Const(1 if ((ideal - phi) % S) == S - 1 else 0, 1) for phi in range(S)])
            if S == 3:
                up_lut = Array([Const(1 if ((ideal - phi) % S) == 1 else 0, 1) for phi in range(S)])
            up_terms.append(edges[i] & up_lut[self.phase])
            down_terms.append(edges[i] & down_lut[self.phase])
        up = _popcount(m, up_terms, "votes_up")
        down = _popcount(m, down_terms, "votes_down")

        # --- loop filter -------------------------------------------------------------------
        T = self.track_threshold
        acc = Signal(signed(8))
        thr = Signal(signed(8))
        m.d.comb += thr.eq(Mux(self.in_packet, T, 1))
        acc_next = Signal(signed(8))
        m.d.comb += acc_next.eq(acc + up.as_signed() - down.as_signed())
        step_up = Signal()
        step_down = Signal()
        m.d.comb += [
            step_up.eq(acc_next >= thr),
            step_down.eq(acc_next <= -thr),
        ]
        with m.If(step_up | step_down):
            sync += acc.eq(0)
        with m.Else():
            # saturate so a burst of noise cannot bank votes
            sync += acc.eq(Mux(acc_next > T, T, Mux(acc_next < -T, -T, acc_next)))

        # --- picks -------------------------------------------------------------------------
        # sel = phase + step + 1 in [0, S+1]: 0 means "phase went to -1", S+1 means "went to S".
        sel = Signal(range(S + 2))
        m.d.comb += sel.eq(self.phase + 1 + step_up - step_down)
        pick = [Signal(name=f"pick{j}") for j in range(B + 1)]
        for j in range(B + 1):
            m.d.comb += pick[j].eq(Array([ext[s + j * S] for s in range(S + 2)])[sel])
        count = Signal(range(B + 2))
        with m.If(sel == 0):
            m.d.comb += count.eq(B + 1)
        with m.Elif(sel == S + 1):
            m.d.comb += count.eq(B - 1)
        with m.Else():
            m.d.comb += count.eq(B)
        # For sel == S+1 the picks are ext[S+1 + j*S], i.e. cur[S + j*S]: B-1 of them are valid.
        # For sel == 0 the picks are ext[j*S]: pick0 = prev_last, then cur[S-1], ...: B+1 valid.
        sync += [
            self.bits.eq(Cat(*pick)),
            self.count.eq(count),
            self.slip_up.eq(sel == S + 1),
            self.slip_down.eq(sel == 0),
            prev_last.eq(cur[W - 1]),
        ]
        with m.If(sel == 0):
            sync += self.phase.eq(S - 1)
        with m.Elif(sel == S + 1):
            sync += self.phase.eq(0)
        with m.Else():
            sync += self.phase.eq(sel - 1)

        # --- activity (digital squelch) ----------------------------------------------------
        limit = self.idle_ui * S
        idle = Signal(range(limit + W + 1))
        with m.If(edges.any()):
            sync += idle.eq(0)
        with m.Elif(idle < limit):
            sync += idle.eq(idle + W)
        m.d.comb += self.activity.eq(idle < limit)
        return m
```

Check the pick geometry once by hand before running: with `sel = phase + 1` (no step) pick j is `ext[phase + 1 + j·S] = cur[phase + j·S]`, the spec's `φ + kS`. With `sel = 0` (stepped to −1) pick 0 is `ext[0] = prev_last` and pick j is `cur[j·S − 1]`, B+1 picks. With `sel = S + 1` pick j is `cur[S + j·S]`, and only j < B−1 stay inside the word — the extra picks are zero padding, which `count` excludes. `ext` needs `W + S + 2` bits so every `Array` index is in range; the padding bits are constant zero.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cdr.py -q`
Expected: all pass (8 + 16 + 4 + 1 + 1 + 1 + 1 = 32 tests). Likely first failures and what they mean:
- static phase fails for some `phase` but not others → the vote direction or the `ideal` offset is mirrored; flip the sign convention once (`(phi - ideal)` vs `(ideal - phi)`) rather than patching individual cases, and confirm with `test_frequency_offset_tracked_over_long_packet`'s slip-direction assertion.
- offset test recovers bits but the slip count is off by a lot → `count` and `phase` wrap handling disagree; print `sel` per cycle for a 100-cycle run.
- jitter 0.15 UI fails while 0.10 passes → acceptable only if the spec's margin note is updated; first try `track_threshold=4`.

- [ ] **Step 5: Commit**

```bash
git add usb2soft/rx/__init__.py usb2soft/rx/cdr.py tests/test_cdr.py
git commit -m "rx: oversampling CDR with vote-based phase pointer and bit slip (3x/4x)"
```

---

### Task 3: Packet decoder (NRZI, SYNC, unstuff, EOP, packer)

**Files:**
- Create: `usb2soft/rx/decoder.py`
- Test: `tests/test_decoder.py`

Runs in the CDR's domain on words of up to `max_bits` (B+1) line bits per cycle. Emits per cycle at most one **event** (`START`, `END`, `ERROR`) and at most one **data byte**; the two never coincide within a word of ≤5 bits (a byte completes 8 bits after the previous one, an EOP needs 7 bits after the last byte boundary, a START needs ≥9 bits before the first byte).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_decoder.py
import random
import pytest
from amaranth.sim import Simulator

from usb2soft.rx.decoder import PacketDecoder, Event
from usb2soft.sim import usbhs


def feed(line_bits, *, chunk=5, gaps=None, activity_drop_at=None):
    """Drive line bits into the decoder ``chunk`` at a time; return the event/byte trace."""
    dut = PacketDecoder(max_bits=5)
    trace = []
    pieces = [line_bits[i:i + chunk] for i in range(0, len(line_bits), chunk)]

    async def tb(ctx):
        ctx.set(dut.activity, 1)
        for n, piece in enumerate(pieces):
            ctx.set(dut.bits, sum(b << i for i, b in enumerate(piece)))
            ctx.set(dut.count, len(piece))
            if activity_drop_at is not None and n >= activity_drop_at:
                ctx.set(dut.activity, 0)
            await ctx.tick()
            if ctx.get(dut.event_valid):
                trace.append(("evt", Event(ctx.get(dut.event))))
            if ctx.get(dut.data_valid):
                trace.append(("data", ctx.get(dut.data)))
        ctx.set(dut.count, 0)
        for _ in range(4):
            await ctx.tick()
            if ctx.get(dut.event_valid):
                trace.append(("evt", Event(ctx.get(dut.event))))
            if ctx.get(dut.data_valid):
                trace.append(("data", ctx.get(dut.data)))

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    return trace


def packets_from(trace):
    pkts, cur = [], None
    for kind, v in trace:
        if kind == "evt" and v == Event.START:
            cur = bytearray()
        elif kind == "data":
            assert cur is not None, "data outside a packet"
            cur.append(v)
        elif kind == "evt" and v == Event.END:
            pkts.append(bytes(cur))
            cur = None
        elif kind == "evt" and v == Event.ERROR:
            pkts.append(None)
            cur = None
    return pkts


@pytest.mark.parametrize("sync", [12, 16, 20, 32])
@pytest.mark.parametrize("chunk", [1, 3, 4, 5])
def test_packet_any_sync_length_and_chunking(sync, chunk):
    payload = bytes(random.Random(sync + chunk).randrange(256) for _ in range(40))
    line = [1] * 10 + usbhs.packet_line_bits(payload, sync_bits=sync) + [1] * 10
    assert packets_from(feed(line, chunk=chunk)) == [payload]


def test_all_ones_payload_is_unstuffed():
    payload = b"\xff" * 32
    line = [1] * 10 + usbhs.packet_line_bits(payload) + [1] * 10
    assert packets_from(feed(line)) == [payload]


def test_zero_payload_and_short_packets():
    for payload in (b"", b"\x5a", b"\x00" * 3, bytes([0x2D, 0x00, 0x10])):   # incl. a token-like one
        line = [1] * 10 + usbhs.packet_line_bits(payload) + [1] * 10
        assert packets_from(feed(line)) == [payload]


def test_back_to_back_packets_with_minimal_gap():
    p1, p2 = bytes(range(8)), bytes(range(8, 24))
    line = [1] * 10 + usbhs.packet_line_bits(p1) + [1] * 8 + usbhs.packet_line_bits(p2) + [1] * 10
    assert packets_from(feed(line)) == [p1, p2]


def test_misaligned_eop_reports_error():
    payload = bytes(range(8))
    line = usbhs.packet_line_bits(payload)
    # corrupt: insert one extra data bit before the EOP so the byte boundary is off by one
    line = [1] * 10 + line[:-8] + [line[-9]] + line[-8:] + [1] * 10
    got = packets_from(feed(line))
    assert got == [None]


def test_activity_loss_mid_packet_reports_error():
    payload = bytes(range(64))
    line = [1] * 10 + usbhs.packet_line_bits(payload)
    trace = feed(line, activity_drop_at=40)
    kinds = [v for k, v in trace if k == "evt"]
    assert kinds[0] == Event.START and kinds[-1] == Event.ERROR


def test_short_zero_run_is_not_a_sync():
    # data 0000001 (six zeros then one) must not start a packet
    line = [1] * 10 + usbhs.nrzi_encode([0] * 6 + [1] + [1] * 20, initial=1)
    assert feed(line) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_decoder.py -q`
Expected: FAIL, `No module named 'usb2soft.rx.decoder'`

- [ ] **Step 3: Implement the decoder**

```python
# usb2soft/rx/decoder.py
"""Word-parallel USB HS packet decoder (spec §4.3), running in the CDR domain.

Input per cycle: up to ``max_bits`` recovered line bits (bit 0 earliest) and their count, plus
the CDR's ``activity`` flag. Output per cycle: at most one event (START/END/ERROR) and at most
one data byte. The chain below is combinational across the bits of one word, with the running
state (previous level, zero run, ones run, packer) registered between cycles.
"""
import enum
from amaranth import Elaboratable, Module, Signal, Mux

__all__ = ["PacketDecoder", "Event"]


class Event(enum.IntEnum):
    NONE = 0
    START = 1
    END = 2
    ERROR = 3


SYNC_ZEROS = 8       # minimum decoded zeros before the SYNC's terminating one
STUFF_RUN = 6
EOP_BITS = 7         # bits held since the last byte boundary when the violation arrives


class PacketDecoder(Elaboratable):
    def __init__(self, *, max_bits=5, domain="sync"):
        self.max_bits = max_bits
        self.domain = domain
        self.bits = Signal(max_bits)
        self.count = Signal(range(max_bits + 1))
        self.activity = Signal()

        self.event_valid = Signal()
        self.event = Signal(Event)
        self.data_valid = Signal()
        self.data = Signal(8)
        self.in_packet = Signal()

    def elaborate(self, platform):
        m = Module()
        sync = m.d[self.domain]
        N = self.max_bits

        # Registered state carried between words.
        last_level = Signal(init=1)
        zero_run = Signal(range(SYNC_ZEROS + 1))
        ones_run = Signal(range(STUFF_RUN + 1))
        acc = Signal(8)                     # packer, LSB = earliest data bit
        acc_n = Signal(range(9))
        in_packet = Signal()

        # Chain values, updated bit by bit.
        c_level, c_zero, c_ones, c_acc, c_accn, c_in = last_level, zero_run, ones_run, acc, acc_n, in_packet
        event = Signal(Event)
        data_valid = Signal()
        data = Signal(8)

        for i in range(N):
            valid = Signal(name=f"v{i}")
            m.d.comb += valid.eq(self.count > i)
            raw = self.bits[i]
            d = Signal(name=f"d{i}")
            m.d.comb += d.eq(~(raw ^ c_level))

            n_level = Signal(name=f"lvl{i}")
            n_zero = Signal(range(SYNC_ZEROS + 1), name=f"zr{i}")
            n_ones = Signal(range(STUFF_RUN + 1), name=f"or{i}")
            n_acc = Signal(8, name=f"acc{i}")
            n_accn = Signal(range(9), name=f"accn{i}")
            n_in = Signal(name=f"in{i}")

            start = valid & ~c_in & d & (c_zero >= SYNC_ZEROS)
            stuffed = c_ones == STUFF_RUN
            violation = valid & c_in & stuffed & d
            drop = valid & c_in & stuffed & ~d
            take = valid & c_in & ~stuffed
            byte_done = take & (c_accn == 7)

            m.d.comb += [
                n_level.eq(Mux(valid, raw, c_level)),
                # zero run only matters outside a packet
                n_zero.eq(Mux(valid & ~c_in,
                              Mux(d, 0, Mux(c_zero == SYNC_ZEROS, c_zero, c_zero + 1)),
                              Mux(valid & c_in, 0, c_zero))),
                n_in.eq(Mux(start, 1, Mux(violation, 0, c_in))),
                n_ones.eq(Mux(start, 1,                     # the SYNC's final one counts
                              Mux(drop | violation, 0,
                                  Mux(take, Mux(d, c_ones + 1, 0), c_ones)))),
                n_accn.eq(Mux(start | violation, 0,
                              Mux(byte_done, 0, Mux(take, c_accn + 1, c_accn)))),
                n_acc.eq(Mux(take, (c_acc >> 1) | (d << 7), c_acc)),
            ]
            with m.If(start):
                m.d.comb += event.eq(Event.START)
            with m.If(violation):
                m.d.comb += event.eq(Mux(c_accn == EOP_BITS, Event.END, Event.ERROR))
            with m.If(byte_done):
                m.d.comb += [data_valid.eq(1), data.eq((c_acc >> 1) | (d << 7))]

            c_level, c_zero, c_ones, c_acc, c_accn, c_in = n_level, n_zero, n_ones, n_acc, n_accn, n_in

        # Loss of activity inside a packet ends it with an error.
        lost = in_packet & ~self.activity & (event == Event.NONE)
        sync += [
            last_level.eq(c_level), zero_run.eq(c_zero), ones_run.eq(c_ones),
            acc.eq(c_acc), acc_n.eq(c_accn),
            in_packet.eq(Mux(lost, 0, c_in)),
            self.event.eq(Mux(lost, Event.ERROR, event)),
            self.event_valid.eq(lost | (event != Event.NONE)),
            self.data_valid.eq(data_valid),
            self.data.eq(data),
        ]
        m.d.comb += self.in_packet.eq(in_packet)
        return m
```

Packer detail: bits arrive LSB-first, so the accumulator shifts right and inserts at bit 7; after 8 bits the byte is complete with bit 0 = first bit. `byte_done` forms the byte from the 7 held bits plus the current one. At the violation, `c_accn == 7` means exactly the EOP's `0111111` is pending, which is discarded.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_decoder.py -q`
Expected: `16 + 1 + 1 + 1 + 1 + 1 + 1 = 22 passed`. If `test_misaligned_eop_reports_error` yields `END` instead of `ERROR`, check that the inserted bit really shifts the boundary (the model inserts a copy of the last data level, i.e. a data 1); if `test_all_ones_payload_is_unstuffed` fails, the stuffed zero is being counted in `acc_n` — `take` must be false when `stuffed`.

- [ ] **Step 5: Commit**

```bash
git add usb2soft/rx/decoder.py tests/test_decoder.py
git commit -m "rx: word-parallel packet decoder (NRZI, SYNC, unstuff, HS EOP, byte packer)"
```

---

### Task 4: UTMI bridge and full RX path

**Files:**
- Create: `usb2soft/rx/bridge.py`
- Modify: `usb2soft/rx/__init__.py` (add `RxPath`)
- Test: `tests/test_rx_path.py`

`RxUTMIBridge` writes one 12-bit FIFO entry per cycle in which the decoder produced anything (`data[8]`, `data_valid`, `start`, `end`, `error`) and drains it in the `usb` domain: `START` raises `rx_active`; a data entry pulses `rx_valid` with `rx_data`; `END` lowers `rx_active` after the entry; `ERROR` pulses `rx_error` and lowers `rx_active`. UTMI requires `rx_active` to be high before the first `rx_valid` — guaranteed because START is always a separate, earlier entry (≥9 bits before the first byte).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rx_path.py
import random
import pytest
from amaranth import Module, Elaboratable
from amaranth.sim import Simulator

from usb2soft.rx import RxPath
from usb2soft.sim import usbhs


class _Harness(Elaboratable):
    def __init__(self, S=4, W=16):
        self.rx = RxPath(samples_per_ui=S, samples_per_word=W, cdr_domain="cdr", usb_domain="usb")

    def elaborate(self, platform):
        m = Module()
        m.submodules.rx = self.rx
        return m


def run_path(samples, *, S=4, W=16, extra_cycles=200):
    dut = _Harness(S, W)
    wds = usbhs.words(samples, W)
    got, active_trace, errors = [], [], 0
    cur = None

    async def feeder(ctx):
        for w in wds:
            ctx.set(dut.rx.samples, w)
            await ctx.tick("cdr")
        ctx.set(dut.rx.samples, 0xFFFF)
        for _ in range(extra_cycles):
            await ctx.tick("cdr")

    async def utmi(ctx):
        nonlocal cur, errors
        prev_active = 0
        for _ in range(len(wds) // 2 + extra_cycles // 2 + 10):
            active = ctx.get(dut.rx.rx_active)
            valid = ctx.get(dut.rx.rx_valid)
            if valid:
                assert active, "rx_valid without rx_active"
                assert prev_active, "rx_valid in the same cycle rx_active rose"
                cur.append(ctx.get(dut.rx.rx_data))
            if active and not prev_active:
                cur = bytearray()
            if prev_active and not active and cur is not None:
                got.append(bytes(cur))
                cur = None
            errors += ctx.get(dut.rx.rx_error)
            prev_active = active
            await ctx.tick("usb")

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6, domain="cdr")
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(feeder)
    sim.add_testbench(utmi, background=True)
    sim.run()
    return got, errors


def stream(payloads, *, ppm, S=4, gap_ui=16, phase=0.7, seed=0, rj_ui=0.0):
    line = [1] * 40
    for p in payloads:
        line += usbhs.packet_line_bits(p) + [1] * gap_ui
    return usbhs.LineSampler(samples_per_ui=S, ppm=ppm, phase=phase, rj_ui=rj_ui, seed=seed).sample(line)


@pytest.mark.parametrize("ppm", [-1000, -500, 0, 500, 1000])
def test_random_packets_at_offset(ppm):
    rng = random.Random(ppm)
    payloads = [bytes(rng.randrange(256) for _ in range(rng.choice([3, 8, 64, 512]))) for _ in range(4)]
    got, errors = run_path(stream(payloads, ppm=ppm))
    assert got == payloads and errors == 0


def test_elastic_buffer_all_zero_payload_fast_host():
    # all-zero data has no stuffing, so bytes arrive at the maximum rate; +500 ppm on top
    payloads = [bytes(1024)]
    got, errors = run_path(stream(payloads, ppm=500), extra_cycles=600)
    assert got == payloads and errors == 0


def test_three_x_variant():
    payloads = [bytes(range(32)), bytes(range(100, 130))]
    got, errors = run_path(stream(payloads, ppm=-300, S=3), S=3, W=12)
    assert got == payloads and errors == 0


def test_jitter_and_noise_between_packets():
    rng = random.Random(4)
    payloads = [bytes(rng.randrange(256) for _ in range(64)) for _ in range(3)]
    line = []
    samples = usbhs.idle_noise(n_ui=80, samples_per_ui=4, toggle_prob=0.3, seed=1)
    for i, p in enumerate(payloads):
        samples += usbhs.LineSampler(samples_per_ui=4, ppm=250, phase=i * 1.3, rj_ui=0.08, seed=i).sample(
            [1] * 8 + usbhs.packet_line_bits(p) + [1] * 8)
        samples += usbhs.idle_noise(n_ui=50, samples_per_ui=4, toggle_prob=0.3, seed=10 + i)
    got, errors = run_path(samples)
    # noise may or may not fabricate junk packets; every real packet must be present and intact
    for p in payloads:
        assert p in got
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_rx_path.py -q`
Expected: FAIL, `cannot import name 'RxPath'`

- [ ] **Step 3: Implement bridge and RxPath**

```python
# usb2soft/rx/bridge.py
"""Move decoder events into the UTMI clock domain and drive the UTMI receive signals."""
from amaranth import Elaboratable, Module, Signal, Cat
from amaranth.lib.fifo import AsyncFIFOBuffered

from .decoder import Event

__all__ = ["RxUTMIBridge"]


class RxUTMIBridge(Elaboratable):
    def __init__(self, *, decoder, cdr_domain, usb_domain, depth=16):
        self.decoder = decoder
        self.cdr_domain = cdr_domain
        self.usb_domain = usb_domain
        self.depth = depth
        self.rx_active = Signal()
        self.rx_valid = Signal()
        self.rx_data = Signal(8)
        self.rx_error = Signal()
        self.overflow = Signal()     # sticky, for debug counters

    def elaborate(self, platform):
        m = Module()
        dec = self.decoder
        m.submodules.fifo = fifo = AsyncFIFOBuffered(width=12, depth=self.depth,
                                                     w_domain=self.cdr_domain, r_domain=self.usb_domain)
        start = dec.event_valid & (dec.event == Event.START)
        end = dec.event_valid & (dec.event == Event.END)
        error = dec.event_valid & (dec.event == Event.ERROR)
        m.d.comb += [
            fifo.w_data.eq(Cat(dec.data, dec.data_valid, start, end, error)),
            fifo.w_en.eq(dec.event_valid | dec.data_valid),
        ]
        with m.If(fifo.w_en & ~fifo.w_rdy):
            m.d[self.cdr_domain] += self.overflow.eq(1)

        r_data, r_dv, r_start, r_end, r_err = fifo.r_data[0:8], fifo.r_data[8], fifo.r_data[9], fifo.r_data[10], fifo.r_data[11]
        m.d.comb += fifo.r_en.eq(1)
        usb = m.d[self.usb_domain]
        usb += [self.rx_valid.eq(0), self.rx_error.eq(0)]
        with m.If(fifo.r_rdy):
            with m.If(r_start):
                usb += self.rx_active.eq(1)
            with m.If(r_dv):
                usb += [self.rx_valid.eq(1), self.rx_data.eq(r_data)]
            with m.If(r_end | r_err):
                usb += self.rx_active.eq(0)
            with m.If(r_err):
                usb += self.rx_error.eq(1)
        return m
```

```python
# usb2soft/rx/__init__.py
"""USB high-speed receive path: oversampling CDR, packet decoder, UTMI bridge."""
from amaranth import Elaboratable, Module, Signal

from .cdr import OversamplingCDR
from .decoder import PacketDecoder, Event
from .bridge import RxUTMIBridge

__all__ = ["RxPath", "OversamplingCDR", "PacketDecoder", "RxUTMIBridge", "Event"]


class RxPath(Elaboratable):
    """samples (cdr domain) -> UTMI rx signals (usb domain)."""
    def __init__(self, *, samples_per_ui=4, samples_per_word=16, cdr_domain="rx_cdr",
                 usb_domain="usb", track_threshold=3):
        self.cdr = OversamplingCDR(samples_per_ui=samples_per_ui, samples_per_word=samples_per_word,
                                   track_threshold=track_threshold, domain=cdr_domain)
        self.decoder = PacketDecoder(max_bits=self.cdr.B + 1, domain=cdr_domain)
        self.bridge = RxUTMIBridge(decoder=self.decoder, cdr_domain=cdr_domain, usb_domain=usb_domain)
        self.samples = self.cdr.samples
        self.rx_active = self.bridge.rx_active
        self.rx_valid = self.bridge.rx_valid
        self.rx_data = self.bridge.rx_data
        self.rx_error = self.bridge.rx_error

    def elaborate(self, platform):
        m = Module()
        m.submodules.cdr = self.cdr
        m.submodules.decoder = self.decoder
        m.submodules.bridge = self.bridge
        m.d.comb += [
            self.decoder.bits.eq(self.cdr.bits),
            self.decoder.count.eq(self.cdr.count),
            self.decoder.activity.eq(self.cdr.activity),
            self.cdr.in_packet.eq(self.decoder.in_packet),
        ]
        return m
```

Note the `rx_valid`/`rx_active` ordering in the bridge: a START entry sets `rx_active` on edge N; the earliest data entry is read on edge N+1 or later, so `rx_valid` appears at least one cycle after `rx_active` rose, satisfying LUNA's token detector (`device.py`/`packet.py` sample `rx_active` first, then `rx_valid`). `END` clears `rx_active` on the edge after the last data byte's `rx_valid`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_rx_path.py -q`
Expected: `8 passed` (5 + 1 + 1 + 1). Then `uv run pytest -q`: everything green (previous 50 + ~70 new).

- [ ] **Step 5: Commit**

```bash
git add usb2soft/rx tests/test_rx_path.py
git commit -m "rx: UTMI bridge (event FIFO into the usb domain) and RxPath composition"
```

---

### Task 5: Margin sweeps and results page

**Files:**
- Create: `tests/test_rx_margins.py` (marked `slow`), `docs/results/2026-09-XX-p2-rx-simulation.md`
- Modify: `pyproject.toml` (register the `slow` marker)

- [ ] **Step 1: Write the sweep test**

```python
# tests/test_rx_margins.py
"""Slow sweeps that measure margins rather than pass/fail a single point. Run with
``uv run pytest -m slow -q -s`` and copy the printed table into docs/results."""
import random
import pytest

from tests.test_cdr import run_cdr, contains, line_for
from usb2soft.sim import usbhs

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("S,W", [(4, 16), (3, 12)])
def test_jitter_margin_table(S, W):
    payload = bytes(random.Random(1).randrange(256) for _ in range(256))
    line = line_for(payload)
    rows = []
    for rj in [0.0, 0.04, 0.08, 0.12, 0.16, 0.20, 0.24]:
        ok = 0
        for seed in range(10):
            samples = usbhs.LineSampler(samples_per_ui=S, ppm=500, rj_ui=rj, seed=seed).sample(line)
            bits, _ = run_cdr(samples, S=S, W=W, in_packet_after=3)
            ok += contains(bits, line)
        rows.append((rj, ok))
        print(f"S={S} rj={rj:.2f} UI rms: {ok}/10 packets intact")
    # spec margin: everything up to 0.12 UI rms must pass
    assert all(ok == 10 for rj, ok in rows if rj <= 0.12)


def test_ppm_limit():
    payload = bytes(random.Random(2).randrange(256) for _ in range(1024))
    line = line_for(payload)
    worst = None
    for ppm in [2000, 4000, 8000, 16000]:
        samples = usbhs.LineSampler(samples_per_ui=4, ppm=ppm, phase=0.5).sample(line)
        bits, _ = run_cdr(samples, S=4, W=16, in_packet_after=3)
        ok = contains(bits, line)
        print(f"ppm={ppm}: {'ok' if ok else 'FAIL'}")
        if ok:
            worst = ppm
    assert worst is not None and worst >= 4000
```

Add to `pyproject.toml` under `[tool.pytest.ini_options]`: `markers = ["slow: long sweeps, run explicitly with -m slow"]` and `addopts = "-ra -m 'not slow'"`.

- [ ] **Step 2: Run the sweeps**

Run: `uv run pytest -m slow -q -s tests/test_rx_margins.py`
Expected: prints the two tables; assertions hold. Record the printed tables verbatim in `docs/results/2026-09-XX-p2-rx-simulation.md` together with the test counts, the chosen `track_threshold`, and any margin that fell short (with the follow-up noted).

- [ ] **Step 3: Update the spec**

In `docs/superpowers/specs/2026-09-06-usb2-soft-phy-design.md` §4.3 replace the first paragraph ("A 2:1 gearbox … 10 bits per 60 MHz cycle …") with: the decoder runs in `rx_cdr` at 120 MHz on the CDR's ≤5-bit words; events and bytes cross to `usb` through a 16-entry async FIFO (`RxUTMIBridge`) which is also the elastic buffer; the diagram in §4 changes accordingly (`OversamplingCDR ──► PacketDecoder ──► event FIFO ──► UTMI rx_*`). Mention the reason (quarter-size prefix logic, no phase assumption).

- [ ] **Step 4: Commit**

```bash
git add tests/test_rx_margins.py pyproject.toml docs
git commit -m "rx: margin sweeps, P2 simulation results, spec §4.3 updated for the 120 MHz decoder"
```

---

### Task 6: Review and finish

- [ ] **Step 1:** `uv run pytest -q` green; `uv run pytest -m slow -q` green.
- [ ] **Step 2:** Request code review (superpowers:requesting-code-review) for the range `p0-bootstrap..HEAD`; fix Critical/Important findings.
- [ ] **Step 3:** Rebase onto `main` if PR #1 has merged (`git rebase origin/main`), push, open PR "P2: RX path in simulation", merge with a merge commit after review.
