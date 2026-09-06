import pytest
from usb2soft.clock.params import solve, ClockSolution, LIMITS


def test_limits_table_matches_ds181():
    assert LIMITS["mmcm"]["-2"].vco == (600e6, 1440e6)
    assert LIMITS["mmcm"]["-1"].vco == (600e6, 1200e6)
    assert LIMITS["pll"]["-2"].vco == (800e6, 1866e6)
    assert LIMITS["pll"]["-1"].vco == (800e6, 1600e6)


def test_netv2_bringup_pll():
    # docs/clocking.md pins the PLL VCO at 1200 MHz; without a target the solver would pick 1800.
    sol = solve(kind="pll", speed="-2", fin=50e6, outputs={"usb": 60e6, "idelay_ref": 300e6}, vco=1200e6)
    assert isinstance(sol, ClockSolution)
    assert sol.divclk == 1 and sol.mult == 24 and sol.vco == 1200e6
    assert sol.outputs["usb"].divide == 20 and sol.outputs["idelay_ref"].divide == 4


def test_default_mmcm_plan():
    sol = solve(kind="mmcm", speed="-2", fin=60e6, vco=960e6,
                outputs={"rx_io": 480e6, "tx_io": 240e6, "rx_cdr": 120e6, "usb": 60e6})
    assert sol.vco == 960e6 and sol.mult == 16
    assert [sol.outputs[k].divide for k in ("rx_io", "tx_io", "rx_cdr", "usb")] == [2, 4, 8, 16]


def test_vco_target_must_be_reachable():
    with pytest.raises(ValueError, match="no solution"):
        solve(kind="mmcm", speed="-2", fin=60e6, vco=1000e6, outputs={"usb": 60e6})   # 1000/60 not integer


def test_phase_is_carried_and_validated():
    sol = solve(kind="mmcm", speed="-2", fin=60e6, vco=960e6,
                outputs={"rx_io": 480e6, "rx_io90": (480e6, 90.0), "rx_cdr": 120e6})
    assert sol.outputs["rx_io90"].phase == 90.0
    with pytest.raises(ValueError, match="phase"):
        # 1/8 VCO period resolution: at divide 8 that is 5.625 deg, so 20 deg is not representable
        solve(kind="mmcm", speed="-2", fin=60e6, vco=960e6, outputs={"a": 480e6, "b": (120e6, 20.0)})


def test_speed_grade_minus_one_rejects_480():
    with pytest.raises(ValueError, match="no solution"):
        solve(kind="mmcm", speed="-1", fin=60e6, outputs={"rx_io": 480e6, "usb": 60e6}, max_bufg=464e6)


def test_minus_one_3x_plan():
    sol = solve(kind="mmcm", speed="-1", fin=60e6, vco=720e6,
                outputs={"rx_io": 360e6, "tx_io": 240e6, "rx_cdr": 120e6, "usb": 60e6}, max_bufg=464e6)
    assert sol.vco == 720e6 and sol.mult == 12


def test_without_target_prefers_highest_vco():
    # integer mult only and 100 must divide the VCO: candidates 800,900,...,1400 -> highest is 1400.
    sol = solve(kind="mmcm", speed="-2", fin=50e6, outputs={"o": 100e6})
    assert sol.vco == 1400e6 and sol.mult == 28 and sol.outputs["o"].divide == 14


def test_period_helpers():
    sol = solve(kind="pll", speed="-2", fin=50e6, outputs={"usb": 60e6})
    assert sol.clkin_period_ns == 20.0
    assert sol.outputs["usb"].frequency == 60e6
