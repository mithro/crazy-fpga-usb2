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
    "payload_data_bits", "packet_line_bits", "LineSampler", "idle_noise", "words", "reference_decode",
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
    samples = list(samples)
    if len(samples) % width:
        samples = samples + [samples[-1]] * (width - len(samples) % width)
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
        while t < end - 1e-9:
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
        payload = bytes(sum(bits[k + j] << j for j in range(8))
                        for k in range(0, len(bits) - len(bits) % 8, 8))
        packets.append(payload)
    return packets
