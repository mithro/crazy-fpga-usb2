# Clocking notes

Numbers from DS181 v1.27 (Artix-7 DC and AC switching characteristics),
column `-2/-2LE` unless stated, extracted 2026-09-06. The NeTV2 boards are
`xc7a35t-fgg484-2` and `xc7a100t-fgg484-2`.

| Limit | -2 | -1 | Table |
|-------|----|----|-------|
| FMAX_BUFG | 628 MHz | 464 MHz | Table 32 |
| FMAX_BUFIO | 680 MHz | 600 MHz | Table 33 |
| FMAX_BUFR | 375 MHz | 315 MHz | Table 34 |
| MMCM VCO | 600–1440 MHz | 600–1200 MHz | Table 37 |
| MMCM PSCLK max | 500 MHz | 450 MHz | Table 37 |
| PLL VCO | 800–1866 MHz | 800–1600 MHz | Table 38 |
| DDR LVDS transmitter via OSERDES | 1250 Mb/s | 950 Mb/s | Table 26 |
| IDELAY tap | 1/(32·2·F_REF): 78 ps at 200 MHz, 52 ps at 300 MHz | | Table 30 |

Precedent for exceeding these: the NeTV2 HDMI input runs its ISERDES at
742.5 MHz on a BUFIO (LiteVideo `S7Clocking`), above the 680 MHz limit, and
locks 1080p60 in production firmware. The design does not rely on that; the
default plan below stays within the data sheet.

## Default plan: 4x oversampling, 480 MHz sampling clock

```
clk50 (J19) ──► PLLE2_ADV ×24 = 1200 MHz VCO
                  ├─ ÷20 → 60 MHz   reference for the MMCM
                  └─ ÷4  → 300 MHz  IDELAYCTRL reference (52 ps taps)
60 MHz ──► MMCME2_ADV ×16 = 960 MHz VCO, all outputs USE_FINE_PS
                  ├─ ÷2  → 480 MHz  BUFG  rx_io  (both ISERDESE2 CLK, DDR → 960 MS/s each)
                  ├─ ÷4  → 240 MHz  BUFG  tx_io  (OSERDESE2 CLK, DDR → 480 Mb/s)
                  ├─ ÷8  → 120 MHz  BUFG  rx_cdr (ISERDESE2 CLKDIV, CDR logic)
                  └─ ÷16 → 60 MHz   BUFG  usb    (OSERDESE2 CLKDIV, UTMI, LUNA)
```

- Both ISERDESE2 inputs go through an IDELAYE2 (as in XAPP523, so the
  insertion delays match): the `O` path at tap 0, the `IBUFDS_DIFF_OUT.OB`
  path delayed by half a sample period (≈521 ps ≈ 10 taps at 52 ps). Both ISERDES share CLK and CLKDIV, so the CLK/CLKDIV phase
  rule of UG471 is trivially met. The interleaved streams give 16 samples per
  120 MHz cycle, 4 per UI.
- Alternative (parameter): second ISERDES clocked by a 90°-shifted 480 MHz
  MMCM output *and* its own 22.5°-shifted 120 MHz CLKDIV (UG471 requires
  NETWORKING-mode CLK and CLKDIV of one ISERDES to be phase aligned; giving
  ISERDES #2 both shifted clocks keeps that rule). The two 16-bit words are
  then combined as a related-clock fabric path. Costs one MMCM output and two
  BUFGs, saves the IDELAYCTRL.

## -1 speed grade plan (Arty A7-35T, `xc7a35ticsg324-1L`)

BUFG ≤ 464 MHz and MMCM VCO ≤ 1200 MHz rule out 480 MHz sampling. Use the 3x
two-ISERDES sampler: PLL 100 MHz × 12 = 1200 MHz VCO → 60 MHz and 300 MHz;
MMCM 60 MHz × 12 = 720 MHz VCO → 360 MHz (`rx_io`, DDR 6:1 → 12 samples per
120 MHz cycle), 240, 120, 60. IDELAY offset half a sample = 694 ps ≈ 13 taps.
- Every fabric domain (`usb`, `rx_cdr`) is an MMCM sibling with an integer
  ratio, so `rx_cdr`→`usb` is a synchronous 2:1 gearbox, not a FIFO.

## 3x variants (measurement only)

- One ISERDESE2, DDR 6:1, CLK 720 MHz on a BUFIO (over the 680 MHz limit),
  CLKDIV 240 MHz (BUFR ÷3), gearbox 2:1 to 120 MHz: 12 samples per cycle.
  MMCM ×24 = 1440 MHz VCO (at the -2 maximum). Vivado only (nextpnr-xilinx
  has no BUFIO/BUFR).
- Two ISERDESE2, DDR 6:1, CLK 360 MHz BUFG, IDELAY offset ≈694 ps
  (13 taps), CLKDIV 120 MHz: 12 samples per cycle, in spec.

## Fine phase shift (P6)

Each PSEN step moves the selected outputs by 1/56 of the VCO period: 18.6 ps
at 960 MHz. A step needs about 12 PSCLK cycles. With PSCLK = 240 MHz (the
`tx_io` clock; limit 500 MHz on -2) that is one step per 50 ns → ≈370 ppm of
pull range. The shift wraps round-robin without limit (UG472), so continuous
stepping is a true frequency offset. All outputs carry `USE_FINE_PS=TRUE` so
the whole fabric and I/O clock tree slews together and the RX/TX/UTMI
relationship is untouched.

## Domain summary

| Domain | Freq | Buffer | Used by |
|--------|------|--------|---------|
| `usb` | 60 MHz | BUFG | UTMI, LUNA, TX encoder, OSERDES CLKDIV, control/UART |
| `rx_cdr` | 120 MHz | BUFG | ISERDES CLKDIV, CDR |
| `rx_io` | 480 MHz | BUFG | ISERDES CLK (both) |
| `tx_io` | 240 MHz | BUFG | OSERDES CLK |
| `idelay_ref` | 300 MHz | BUFG | IDELAYCTRL (one per sampler bank) |
| `rx_io90` (fallback only) | 480 MHz @ 90° | BUFG | ISERDES #2 CLK in the two-phase variant |
| `rx_cdr90` (fallback only) | 120 MHz @ 22.5° | BUFG | ISERDES #2 CLKDIV in the two-phase variant |

Resets: `~locked` of the MMCM through `ResetSynchronizer` into each domain;
the PLL lock gates the MMCM reset. ISERDES/OSERDES `RST` is held for a few
CLKDIV cycles after lock.
