"""Slow sweeps that measure margins rather than pass/fail a single point. Run with
``uv run pytest -m slow -q -s tests/test_rx_margins.py`` and copy the printed tables into
docs/results."""
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
    for rj in [0.0, 0.04, 0.08, 0.10, 0.12, 0.16, 0.20]:
        ok = 0
        for seed in range(10):
            samples = usbhs.LineSampler(samples_per_ui=S, ppm=500, rj_ui=rj, seed=seed).sample(line)
            bits, _ = run_cdr(samples, S=S, W=W, in_packet_after=3)
            ok += contains(bits, line)
        rows.append((rj, ok))
        print(f"S={S} rj={rj:.2f} UI rms: {ok}/10 packets (2 kbit) intact")
    # required margin: everything up to 0.08 UI rms must pass at 4x
    if S == 4:
        assert all(ok == 10 for rj, ok in rows if rj <= 0.08)


def test_ppm_limit():
    payload = bytes(random.Random(2).randrange(256) for _ in range(1024))
    line = line_for(payload)
    worst = None
    for ppm in [2000, 4000, 8000, 16000, 32000]:
        samples = usbhs.LineSampler(samples_per_ui=4, ppm=ppm, phase=0.5).sample(line)
        bits, _ = run_cdr(samples, S=4, W=16, in_packet_after=3)
        ok = contains(bits, line)
        print(f"ppm={ppm}: {'ok' if ok else 'FAIL'}")
        if ok:
            worst = ppm
    assert worst is not None and worst >= 4000


@pytest.mark.parametrize("threshold", [1, 2, 3, 4, 6])
def test_track_threshold_versus_jitter(threshold):
    payload = bytes(random.Random(3).randrange(256) for _ in range(256))
    line = line_for(payload)
    for rj in [0.08, 0.10]:
        ok = 0
        for seed in range(10):
            samples = usbhs.LineSampler(samples_per_ui=4, ppm=500, rj_ui=rj, seed=seed).sample(line)
            bits, _ = run_cdr(samples, S=4, W=16, in_packet_after=3, track_threshold=threshold)
            ok += contains(bits, line)
        print(f"track_threshold={threshold} rj={rj:.2f}: {ok}/10")
