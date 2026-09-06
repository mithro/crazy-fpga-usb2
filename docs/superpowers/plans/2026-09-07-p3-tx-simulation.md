# P3 TX Path in Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A simulated USB 2.0 high-speed transmit path — UTMI `tx_data`/`tx_valid`/`tx_ready` in, 480 Mbit/s line bits plus a per-bit driver-enable out — whose output decodes bit-exactly with the P2 wire model and round-trips through the P2 `RxPath` at ±ppm offsets.

**Architecture:** `TxUTMIBridge` (60 MHz `usb` domain) accepts UTMI bytes into a shallow `AsyncFIFOBuffered` (depth 4) so `tx_ready` still tracks the real line rate, and marks the end of a packet when `tx_valid` falls. `PacketEncoder` (120 MHz `tx_cdr` domain, 4 line bits per cycle) emits SYNC (32 bits), bit-stuffed NRZI data, then the HS EOP `01111111` in NRZ, driving `line[4]` and `oe[4]` (bit-granular output enable). `TxPath` composes them. The OSERDESE2 wrapper (`DATA_WIDTH=4`, `DDR`, `TRISTATE_WIDTH=4`, CLK 240 MHz, CLKDIV 120 MHz) lands in P4 with the other primitives.

**Why 4 bits at 120 MHz rather than 8 bits at 60 MHz (spec §4.4 revision):** OSERDESE2 allows `TRISTATE_WIDTH=4` only with `DATA_WIDTH=4`, so the 4:1 DDR configuration gives per-bit tristate and the driver turns off exactly after the last EOP bit. The 8:1 configuration forces `TRISTATE_WIDTH=1`, i.e. byte-granular turn-off, which dribbles up to seven extra bits onto the bus after the EOP. The 120 MHz, 4-bit encoder is also smaller than an 8-bit one and mirrors the RX decoder's word width. The spec §4.4 text is updated in Task 4.

**Tech Stack:** Amaranth 0.5.9, pytest, the P2 wire model (`usb2soft.sim.usbhs`) and `RxPath`.

Branch: continue on `p2-rx-sim` after PR #2 merges → new worktree `.worktrees/p3-tx-sim` from `main`.

---

## File structure

| File | Responsibility |
|------|----------------|
| `usb2soft/tx/__init__.py` | `TxPath` composition + re-exports |
| `usb2soft/tx/encoder.py` | `PacketEncoder`: byte stream + end marker → SYNC/stuffed NRZI/EOP as 4 line bits + 4 oe bits per cycle |
| `usb2soft/tx/bridge.py` | `TxUTMIBridge`: UTMI tx_* (usb) → FIFO → encoder byte interface (tx_cdr) |
| `tests/test_encoder.py`, `tests/test_tx_path.py` | unit and loop tests |
| `docs/results/2026-09-XX-p3-tx-simulation.md` | latencies (tx_valid → first line bit, last byte → EOP end), tx_ready cadence, loop results |

Conventions as in P2: bit 0 of `line`/`oe` is the earliest bit; line 1 = J.

---

### Task 1: Packet encoder

**Files:** create `usb2soft/tx/__init__.py` (docstring), `usb2soft/tx/encoder.py`; test `tests/test_encoder.py`.

Interface (`domain="sync"` default, 4 bits per cycle):

- inputs: `byte_valid`, `byte[8]`, `byte_end` (this entry is the end-of-packet marker, no data), output `byte_ready` (strobe when an entry is consumed);
- outputs: `line[4]`, `oe[4]`, `busy`.

Behaviour: `IDLE` (oe = 0, line = J) → when an entry with data is available: `SYNC` (32 line bits KJKJ…KK, 8 cycles) → `DATA`: a stuffed-bit buffer (up to 16 bits) is topped up one byte at a time (byte → 8 + up to 2 stuffed zeros, via an 8-stage chain carrying `ones_run`); each cycle 4 bits leave the buffer, NRZI-encoded with a carried level → when the end marker is at the head and the buffer is empty: `EOP`: 8 line bits = inverse of last level, then 7 identical (no stuffing), oe per bit, then `IDLE`. If the buffer would underrun in `DATA` (MAC too slow), the encoder treats it as end of packet after emitting what it has (UTMI: deasserting `tx_valid` ends the packet; a starved FIFO is equivalent), and sets a sticky `underrun` flag for debug.

- [ ] **Step 1: tests**

```python
# tests/test_encoder.py
import random
import pytest
from amaranth.sim import Simulator
from usb2soft.tx.encoder import PacketEncoder
from usb2soft.sim import usbhs


def drive(payloads, *, gap_cycles=8, stall_every=None):
    """Feed packets as byte entries (+ end markers); return (line_bits, oe_bits, ready_cycles)."""
    dut = PacketEncoder()
    line, oe, ready_at = [], [], []
    entries = []
    for p in payloads:
        entries += [(b, 0) for b in p] + [(0, 1)]

    async def feeder(ctx):
        idx = 0
        cycle = 0
        while idx < len(entries):
            b, end = entries[idx]
            stalled = stall_every and (cycle % stall_every == 0)
            ctx.set(dut.byte_valid, 0 if stalled else 1)
            ctx.set(dut.byte, b)
            ctx.set(dut.byte_end, end)
            await ctx.tick()
            cycle += 1
            if ctx.get(dut.byte_ready):
                ready_at.append(cycle)
                idx += 1
                if end:
                    ctx.set(dut.byte_valid, 0)
                    for _ in range(gap_cycles):
                        await ctx.tick()
                        cycle += 1
        ctx.set(dut.byte_valid, 0)
        for _ in range(12):
            await ctx.tick()

    async def sampler(ctx):
        while True:
            l, o = ctx.get(dut.line), ctx.get(dut.oe)
            for i in range(4):
                line.append((l >> i) & 1)
                oe.append((o >> i) & 1)
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(feeder)
    sim.add_testbench(sampler, background=True)
    sim.run()
    return line, oe, ready_at


def driven_segments(line, oe):
    """Split the sampled stream into the line bits emitted while oe was high."""
    segs, cur = [], []
    for l, o in zip(line, oe):
        if o:
            cur.append(l)
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return segs


@pytest.mark.parametrize("payload", [b"", b"\x5a", bytes(range(16)), b"\xff" * 40,
                                     bytes(random.Random(1).randrange(256) for _ in range(300))])
def test_packet_bits_match_model_exactly(payload):
    line, oe, _ = drive([payload])
    segs = driven_segments(line, oe)
    assert len(segs) == 1
    assert segs[0] == usbhs.packet_line_bits(payload, sync_bits=32)


def test_two_packets_and_reference_decode():
    p1, p2 = bytes(range(8)), b"\x00\xff" * 10
    line, oe, _ = drive([p1, p2], gap_cycles=16)
    segs = driven_segments(line, oe)
    assert [usbhs.reference_decode([1] * 8 + s + [1] * 8) for s in segs] == [[p1], [p2]]


def test_oe_is_low_between_packets_and_bit_exact_at_eop():
    payload = bytes([0x0F, 0xF0, 0xAA])
    line, oe, _ = drive([payload])
    expected = usbhs.packet_line_bits(payload)
    first = oe.index(1)
    assert oe[first:first + len(expected)] == [1] * len(expected)
    assert oe[first + len(expected)] == 0          # driver off on the very next bit


def test_stalled_source_ends_packet_and_flags_underrun():
    payload = bytes(range(64))
    dut_line, dut_oe, _ = drive([payload], stall_every=2)     # source valid only every other cycle
    # every other cycle is not enough to sustain 4 bits/cycle: the encoder finishes the packet
    segs = driven_segments(dut_line, dut_oe)
    assert segs and usbhs.reference_decode([1] * 8 + segs[0] + [1] * 8)   # a well-formed (shorter) packet
```

The last test needs `underrun` exposed; add `assert` on it by returning the dut from `drive` (adapt `drive` to return `dut` too) — implementer's choice, but the sticky flag must be observable.

- [ ] **Step 2–4:** implement, run, commit `tx: packet encoder (SYNC, stuffing, NRZI, HS EOP) with per-bit output enable`.

Implementation notes for the encoder chain: stuffing a byte given `ones_run` in: for each of the 8 bits produce (bit, then a stuffed 0 if the run hits 6); output up to 10 bits and the new run. Use a fixed-width 10-bit vector plus a length. The buffer is a 20-bit shift register with a fill count; refill when `fill <= 10` and a data entry is present; NRZI over the 4 outgoing bits with a carried level `lvl`: `line[i] = lvl_i`, where `lvl_{i+1} = lvl_i ^ ~d_i`.

---

### Task 2: UTMI TX bridge and TxPath

**Files:** create `usb2soft/tx/bridge.py`, extend `usb2soft/tx/__init__.py`; test `tests/test_tx_path.py`.

`TxUTMIBridge(encoder, usb_domain, tx_domain, depth=4)`: `AsyncFIFOBuffered(width=9)`; in `usb`: `w_en = tx_valid & w_rdy` with data `(tx_data, 0)`; when `tx_valid` falls (previous cycle high, now low) write `(0, end=1)` — this must not be lost if the FIFO is full: hold a pending-end flag until written. `tx_ready = w_rdy & tx_valid & ~pending_end`. In `tx` domain the FIFO output feeds the encoder's `byte_valid/byte/byte_end`, `r_en = encoder.byte_ready`.

`TxPath(tx_domain="tx_cdr", usb_domain="usb")` composes bridge + encoder and exposes `tx_data/tx_valid/tx_ready` (usb) and `line/oe` (tx domain).

Tests (`tests/test_tx_path.py`), two clock domains as in `tests/test_rx_path.py`:
- UTMI byte source (LUNA-style: holds `tx_valid` high with a byte, advances on `tx_ready`, drops `tx_valid` after the last byte) sends random packets; the sampled `line` (while `oe`) equals `usbhs.packet_line_bits(payload)` for each.
- **Round trip**: `TxPath` line bits → `usbhs.LineSampler(ppm=±500)` → `RxPath` → UTMI rx bytes == tx bytes, for packets of 0–512 bytes; also 3x.
- `tx_ready` cadence: over a 512-byte all-zero payload, tx_ready is high on ≥95 % of cycles once started (line rate = 1 byte per 60 MHz cycle); over `\xff` payload it drops to ≈ 6/7 (stuffing).
- Latency measurement (printed and recorded): cycles from first `tx_valid` to first `oe` bit; from `tx_valid` falling to last `oe` bit.

Commit `tx: UTMI bridge with shallow FIFO and TxPath composition; TX->RX loop tests`.

---

### Task 3: Turnaround budget check

Add `tests/test_turnaround.py`: RX packet ends (last line bit) → a MAC model asserts `tx_valid` after N `usb` cycles → first TX line bit; assert the PHY's own contribution (RX EOP detection + bridge + TX bridge + SYNC start) is ≤ 24 `usb` cycles (192 bit times) with N = 0, and print the actual figure for the results page.

---

### Task 4: Docs, review, PR

- `docs/results/2026-09-XX-p3-tx-simulation.md` with the measured latencies and loop results.
- Spec §4.4: replace the 8:1/TRISTATE_WIDTH=1/byte-granular text with the 4:1 DDR, `TRISTATE_WIDTH=4`, 120 MHz encoder description and the reason; keep the T-path latency note (still needed: the OSERDES T inputs go through the same 4:1 serialiser so they align with data by construction, which removes the fabric delay matching step — state that).
- Code review (superpowers:requesting-code-review), fixes, PR "P3: TX path in simulation", merge.
