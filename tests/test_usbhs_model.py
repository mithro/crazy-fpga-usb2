import random
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
    # 4000 UI * 500e-6 = 2 UI = 8 samples fewer (16000/1.0005 = 15992.004, so +-1 for the boundary)
    assert abs(len(s) - (4 * len(long) - 8)) <= 1


def test_sampler_jitter_and_noise_are_deterministic():
    line = usbhs.packet_line_bits(b"\x55" * 8)
    a = usbhs.LineSampler(samples_per_ui=4, rj_ui=0.05, seed=7).sample(line)
    b = usbhs.LineSampler(samples_per_ui=4, rj_ui=0.05, seed=7).sample(line)
    assert a == b and len(a) == 4 * len(line)
    noise = usbhs.idle_noise(n_ui=100, samples_per_ui=4, toggle_prob=0.3, seed=3)
    assert len(noise) == 400 and 0 < sum(noise) < 400


def test_words_groups_samples_earliest_first():
    assert usbhs.words([1, 0, 0, 1, 1, 1, 0, 0], 4) == [0b1001, 0b0011]   # bit 0 = earliest
