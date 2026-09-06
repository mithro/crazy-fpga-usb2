"""7-series PLLE2_ADV / MMCME2_ADV wrappers driven by a ``ClockSolution``.

Every output is buffered by a BUFG; the feedback path is also buffered so the outputs are
phase-aligned with the input clock (the arrangement used by luna-boards and LiteVideo).
``locked`` is the raw LOCKED output; consumers must synchronise it into their domains (see
``usb2soft.clock.netv2``).
"""
from amaranth import Elaboratable, Module, Signal, Instance, Const

__all__ = ["PLLE2", "MMCME2"]


class _ClockManager(Elaboratable):
    primitive = None       # "PLLE2_ADV" or "MMCME2_ADV"
    mult_param = None      # "CLKFBOUT_MULT" or "CLKFBOUT_MULT_F"
    max_outputs = None
    has_ps_ports = False   # MMCM only

    def __init__(self, solution, *, clkin, reset=None, fine_ps=False, ps_clk=None,
                 ps_en=None, ps_incdec=None, ps_done=None):
        if len(solution.outputs) > self.max_outputs:
            raise ValueError(f"{self.primitive} has only {self.max_outputs} outputs")
        if fine_ps and ps_clk is None:
            raise ValueError("fine_ps=True needs a ps_clk (PSCLK) signal")
        self.solution = solution
        self.clkin = clkin
        self.reset = Const(0) if reset is None else reset
        self.fine_ps = fine_ps
        self.ps_clk = ps_clk if ps_clk is not None else Const(0)
        self.ps_en = ps_en if ps_en is not None else Const(0)
        self.ps_incdec = ps_incdec if ps_incdec is not None else Const(0)
        self.ps_done = ps_done if ps_done is not None else Signal(name="ps_done_unused")
        self.locked = Signal()
        self.clocks = {name: Signal(name=f"clk_{name}") for name in solution.outputs}

    def _extra_params(self):
        return {}

    def elaborate(self, platform):
        m = Module()
        sol = self.solution
        fb_out = Signal()
        fb_in = Signal()
        raw = {name: Signal(name=f"raw_{name}") for name in sol.outputs}

        fractional = self.mult_param.endswith("_F")
        params = {
            "p_BANDWIDTH": "OPTIMIZED",
            "p_COMPENSATION": "ZHOLD",
            "p_STARTUP_WAIT": "FALSE",
            "p_DIVCLK_DIVIDE": sol.divclk,
            f"p_{self.mult_param}": float(sol.mult) if fractional else sol.mult,
            "p_CLKFBOUT_PHASE": 0.0,
            "p_CLKIN1_PERIOD": sol.clkin_period_ns,
            "p_REF_JITTER1": 0.01,
        }
        params.update(self._extra_params())
        ports = {
            "i_CLKIN1": self.clkin,
            "i_CLKIN2": Const(0),
            "i_CLKINSEL": Const(1),
            "i_CLKFBIN": fb_in,
            "o_CLKFBOUT": fb_out,
            "i_RST": self.reset,
            "i_PWRDWN": Const(0),
            "o_LOCKED": self.locked,
            # DRP tied off.
            "i_DADDR": Const(0, 7), "i_DCLK": Const(0), "i_DEN": Const(0),
            "i_DI": Const(0, 16), "i_DWE": Const(0),
        }
        for index, (name, setting) in enumerate(sol.outputs.items()):
            div_param = f"p_CLKOUT{index}_DIVIDE"
            if fractional and index == 0:
                params[div_param + "_F"] = float(setting.divide)
            else:
                params[div_param] = setting.divide
            params[f"p_CLKOUT{index}_PHASE"] = setting.phase
            params[f"p_CLKOUT{index}_DUTY_CYCLE"] = 0.5
            if self.fine_ps:
                params[f"p_CLKOUT{index}_USE_FINE_PS"] = "TRUE"
            ports[f"o_CLKOUT{index}"] = raw[name]
        if self.has_ps_ports:
            ports.update({
                "i_PSCLK": self.ps_clk, "i_PSEN": self.ps_en,
                "i_PSINCDEC": self.ps_incdec, "o_PSDONE": self.ps_done,
            })
        m.submodules.cm = Instance(self.primitive, **params, **ports)
        m.submodules.fb_bufg = Instance("BUFG", i_I=fb_out, o_O=fb_in)
        for name in sol.outputs:
            m.submodules[f"bufg_{name}"] = Instance("BUFG", i_I=raw[name], o_O=self.clocks[name])
        return m


class PLLE2(_ClockManager):
    primitive = "PLLE2_ADV"
    mult_param = "CLKFBOUT_MULT"
    max_outputs = 6

    def __init__(self, solution, *, clkin, reset=None):
        if solution.kind != "pll":
            raise ValueError("PLLE2 needs a 'pll' solution")
        super().__init__(solution, clkin=clkin, reset=reset)


class MMCME2(_ClockManager):
    primitive = "MMCME2_ADV"
    mult_param = "CLKFBOUT_MULT_F"
    max_outputs = 7
    has_ps_ports = True

    def __init__(self, solution, *, clkin, reset=None, fine_ps=False, **ps):
        if solution.kind != "mmcm":
            raise ValueError("MMCME2 needs an 'mmcm' solution")
        super().__init__(solution, clkin=clkin, reset=reset, fine_ps=fine_ps, **ps)

    def _extra_params(self):
        return {"p_SS_EN": "FALSE"}
