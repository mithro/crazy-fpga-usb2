"""Pure-Python PLL/MMCM parameter solver for 7-series (no HDL here, so it is exhaustively testable).

Limits from DS181 v1.27 (Table 37 MMCM, Table 38 PLL). Only integer feedback multipliers and
integer output dividers are used; fractional modes are deliberately not supported (they add
jitter and are not available on the PLLE2).
"""
from dataclasses import dataclass, field

__all__ = ["Limits", "LIMITS", "OutputSetting", "ClockSolution", "solve"]


@dataclass(frozen=True)
class Limits:
    vco: tuple          # (min_hz, max_hz)
    fin: tuple          # (min_hz, max_hz)
    mult: tuple = (2, 64)
    divclk: tuple = (1, 106)
    out_divide: tuple = (1, 128)


LIMITS = {
    "mmcm": {
        "-3": Limits(vco=(600e6, 1600e6), fin=(10e6, 800e6)),
        "-2": Limits(vco=(600e6, 1440e6), fin=(10e6, 800e6)),
        "-1": Limits(vco=(600e6, 1200e6), fin=(10e6, 800e6)),
    },
    "pll": {
        "-3": Limits(vco=(800e6, 2133e6), fin=(19e6, 800e6), divclk=(1, 56)),
        "-2": Limits(vco=(800e6, 1866e6), fin=(19e6, 800e6), divclk=(1, 56)),
        "-1": Limits(vco=(800e6, 1600e6), fin=(19e6, 800e6), divclk=(1, 56)),
    },
}

# Output phase is quantised to 1/8 of a VCO period (UG472, static phase shift resolution).
_PHASE_STEPS_PER_VCO_PERIOD = 8


@dataclass(frozen=True)
class OutputSetting:
    frequency: float
    divide: int
    phase: float = 0.0     # degrees of the *output* period


@dataclass(frozen=True)
class ClockSolution:
    kind: str
    fin: float
    divclk: int
    mult: int
    vco: float
    outputs: dict = field(default_factory=dict)

    @property
    def clkin_period_ns(self):
        return 1e9 / self.fin

    @property
    def pfd(self):
        return self.fin / self.divclk


def _normalise(outputs):
    result = {}
    for name, spec in outputs.items():
        if isinstance(spec, tuple):
            freq, phase = spec
        else:
            freq, phase = spec, 0.0
        result[name] = (float(freq), float(phase))
    return result


def solve(*, kind, speed, fin, outputs, vco=None, max_bufg=None):
    """Find integer (divclk, mult, per-output divide) settings.

    ``outputs`` maps a name to a frequency in Hz or to ``(frequency, phase_degrees)``.
    ``vco`` optionally pins the VCO frequency (the documented clock plans do this so a
    build never silently changes plan); without it the highest in-range VCO wins.
    ``max_bufg`` optionally rejects any output above the BUFG limit for the speed grade.
    Raises ValueError with a message containing "no solution" when nothing fits, or
    containing "phase" when a requested phase is not representable.
    """
    lim = LIMITS[kind][speed]
    wanted = _normalise(outputs)
    if not lim.fin[0] <= fin <= lim.fin[1]:
        raise ValueError(f"no solution: input {fin/1e6:.3f} MHz outside {lim.fin}")
    if max_bufg is not None:
        too_fast = [n for n, (f, _) in wanted.items() if f > max_bufg]
        if too_fast:
            raise ValueError(f"no solution: outputs above BUFG limit {max_bufg/1e6:.0f} MHz: {too_fast}")

    candidates = []
    for divclk in range(lim.divclk[0], lim.divclk[1] + 1):
        pfd = fin / divclk
        for mult in range(lim.mult[0], lim.mult[1] + 1):
            vco_hz = pfd * mult
            if not lim.vco[0] <= vco_hz <= lim.vco[1]:
                continue
            if vco is not None and abs(vco_hz - vco) > 1e-3:
                continue
            settings = {}
            ok = True
            for name, (freq, phase) in wanted.items():
                divide = vco_hz / freq
                if abs(divide - round(divide)) > 1e-9 or not lim.out_divide[0] <= round(divide) <= lim.out_divide[1]:
                    ok = False
                    break
                divide = int(round(divide))
                # phase resolution: 360 deg / (divide * 8)
                step = 360.0 / (divide * _PHASE_STEPS_PER_VCO_PERIOD)
                if abs(phase / step - round(phase / step)) > 1e-9:
                    raise ValueError(
                        f"phase {phase} deg on {name!r} is not a multiple of {step:.4f} deg "
                        f"(1/8 VCO period at divide {divide})")
                settings[name] = OutputSetting(frequency=freq, divide=divide, phase=phase)
            if ok:
                candidates.append(ClockSolution(kind=kind, fin=fin, divclk=divclk, mult=mult,
                                                vco=vco_hz, outputs=settings))
    if not candidates:
        raise ValueError(f"no solution for {kind} {speed} fin={fin/1e6:.3f} MHz vco={vco} outputs={wanted}")
    # Prefer the fewest input dividers (highest PFD frequency), then the highest VCO.
    candidates.sort(key=lambda s: (s.divclk, -s.vco))
    return candidates[0]
