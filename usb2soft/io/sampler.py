"""4x oversampling front-end for one differential pair (spec §4.1, docs/clocking.md).

``IBUFDS_DIFF_OUT`` feeds two ILOGIC paths. Both go through an IDELAYE2 (VAR_LOAD, 300 MHz
reference, 52 ps taps): the ``O`` path at tap 0, the inverted ``OB`` path delayed by half a sample
period (default 10 taps ≈ 0.52 ns). Two ISERDESE2 in NETWORKING DDR 8:1 mode share CLK (480 MHz)
and CLKDIV (120 MHz), so each delivers 8 samples per CLKDIV cycle; interleaved they give 16
samples per cycle, 4 per bit.

The IDELAYE2 tap count only changes on an ``LD`` pulse: ``IdelayLoader`` issues one once the
IDELAYCTRL reports ``rdy`` and again whenever ``load_taps`` is pulsed. Inversions (pair swap,
complementary OB path, CLKB) are expressed with the primitives' ``IS_*_INVERTED`` attributes so
that they do not depend on the tool pulling fabric inverters into the IOB (nextpnr-xilinx does not).

Sample order conventions (not yet verified on hardware, both are parameters):

- ``q_reversed=True``: ISERDESE2 Q8 holds the oldest sample, Q1 the newest (LiteVideo mapping).
- ``slave_first=True``: the delayed path captures the signal from *earlier* in time at the same
  clock edge, so the delayed sample precedes the main sample: word = [s0, m0, s1, m1, ...].

The primitives cannot be simulated; tests inspect the RTLIL.
"""
from amaranth import Elaboratable, Module, Signal, Instance, Cat, Const, ClockSignal, ResetSignal

__all__ = ["Sampler", "IdelayCtrl", "IdelayLoader"]


class IdelayCtrl(Elaboratable):
    """One IDELAYCTRL per I/O bank that hosts IDELAYE2s, on the 300 MHz reference."""
    def __init__(self, ref_domain="idelay_ref"):
        self.ref_domain = ref_domain
        self.rdy = Signal()

    def elaborate(self, platform):
        m = Module()
        m.submodules.ctrl = Instance("IDELAYCTRL",
            i_REFCLK=ClockSignal(self.ref_domain),
            i_RST=ResetSignal(self.ref_domain),
            o_RDY=self.rdy,
        )
        return m


class IdelayLoader(Elaboratable):
    """One ``ld`` pulse when ``rdy`` (IDELAYCTRL ready) is first seen, and one per ``load``
    request; runs in the IDELAYE2's ``C`` clock domain."""
    def __init__(self, domain="rx_cdr"):
        self.domain = domain
        self.rdy = Signal()
        self.load = Signal()
        self.ld = Signal()

    def elaborate(self, platform):
        m = Module()
        done = Signal()
        m.d.comb += self.ld.eq(self.rdy & (~done | self.load))
        with m.If(self.ld):
            m.d[self.domain] += done.eq(1)
        return m


class Sampler(Elaboratable):
    def __init__(self, port, *, io_domain="rx_io", div_domain="rx_cdr", delay_taps=10,
                 q_reversed=True, slave_first=True, refclk_mhz=300.0):
        self.port = port
        self.io_domain = io_domain
        self.div_domain = div_domain
        self.q_reversed = q_reversed
        self.slave_first = slave_first
        self.refclk_mhz = refclk_mhz
        self.delay_taps = Signal(5, init=delay_taps)      # run-time loadable (eye alignment hook)
        self.load_taps = Signal()                           # pulse to (re)apply delay_taps
        self.rdy = Signal(init=1)                           # connect to IdelayCtrl.rdy
        self.samples = Signal(16)

    def _idelay(self, m, name, din, taps, load, invert):
        dout = Signal(name=f"{name}_dly")
        m.submodules[name] = Instance("IDELAYE2",
            p_IDELAY_TYPE="VAR_LOAD", p_DELAY_SRC="IDATAIN", p_REFCLK_FREQUENCY=self.refclk_mhz,
            p_SIGNAL_PATTERN="DATA", p_HIGH_PERFORMANCE_MODE="TRUE", p_CINVCTRL_SEL="FALSE",
            p_PIPE_SEL="FALSE", p_IDELAY_VALUE=0, p_IS_IDATAIN_INVERTED=int(invert),
            i_C=ClockSignal(self.div_domain), i_LD=load, i_CE=Const(0), i_INC=Const(0),
            i_CNTVALUEIN=taps, i_LDPIPEEN=Const(0), i_REGRST=Const(0), i_CINVCTRL=Const(0),
            i_DATAIN=Const(0), i_IDATAIN=din, o_DATAOUT=dout,
        )
        return dout

    def _iserdes(self, m, name, ddly):
        q = [Signal(name=f"{name}_q{i}") for i in range(1, 9)]
        m.submodules[name] = Instance("ISERDESE2",
            p_DATA_RATE="DDR", p_DATA_WIDTH=8, p_INTERFACE_TYPE="NETWORKING", p_IOBDELAY="IFD",
            p_NUM_CE=1, p_SERDES_MODE="MASTER", p_OFB_USED="FALSE",
            p_DYN_CLKDIV_INV_EN="FALSE", p_DYN_CLK_INV_EN="FALSE", p_IS_CLKB_INVERTED=1,
            p_INIT_Q1=0, p_INIT_Q2=0, p_INIT_Q3=0, p_INIT_Q4=0,
            p_SRVAL_Q1=0, p_SRVAL_Q2=0, p_SRVAL_Q3=0, p_SRVAL_Q4=0,
            i_DDLY=ddly, i_D=Const(0),
            i_CLK=ClockSignal(self.io_domain), i_CLKB=ClockSignal(self.io_domain),
            i_CLKDIV=ClockSignal(self.div_domain), i_CLKDIVP=Const(0),
            i_RST=ResetSignal(self.div_domain), i_CE1=Const(1), i_CE2=Const(1),
            i_BITSLIP=Const(0), i_OCLK=Const(0), i_OCLKB=Const(0), i_OFB=Const(0),
            i_SHIFTIN1=Const(0), i_SHIFTIN2=Const(0), i_DYNCLKDIVSEL=Const(0), i_DYNCLKSEL=Const(0),
            **{f"o_Q{i + 1}": q[i] for i in range(8)},
        )
        # Oldest sample first.
        return Cat(*reversed(q)) if self.q_reversed else Cat(*q)

    def elaborate(self, platform):
        m = Module()
        o = Signal()
        ob = Signal()
        m.submodules.ibuf = Instance("IBUFDS_DIFF_OUT", i_I=self.port.p, i_IB=self.port.n, o_O=o, o_OB=ob)
        inv = bool(self.port.invert[0]) if hasattr(self.port, "invert") else False
        # Main path: invert only for a swapped pair. Slave path: OB is the complement, so invert
        # unless the pair is swapped. Both via IS_IDATAIN_INVERTED (no fabric logic in the IOB path).
        m.submodules.loader = loader = IdelayLoader(self.div_domain)
        m.d.comb += [loader.rdy.eq(self.rdy), loader.load.eq(self.load_taps)]
        main_dly = self._idelay(m, "idelay_main", o, Const(0, 5), loader.ld, inv)
        slave_dly = self._idelay(m, "idelay_slave", ob, self.delay_taps, loader.ld, not inv)
        main = self._iserdes(m, "iserdes_main", main_dly)
        slave = self._iserdes(m, "iserdes_slave", slave_dly)

        first, second = (slave, main) if self.slave_first else (main, slave)
        m.d.comb += self.samples.eq(Cat(*[bit for i in range(8) for bit in (first[i], second[i])]))
        return m
