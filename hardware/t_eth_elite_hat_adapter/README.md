# T-ETH-Elite ↔ Raspberry Pi HAT pass-through adapter

A passive, 40-pin, pin-1-to-pin-1 adapter board. It lets a genuine Raspberry Pi
HAT — specifically the one this was built for, **TSN Lab's Microchip
LAN8650-based "10Base-T1S HAT"** (devicemart.co.kr #15980354, Raspberry-Pi-only
by design) — physically and electrically plug onto a
[LilyGO T-ETH-Elite](https://github.com/Xinyuan-LilyGO/LilyGO-T-ETH-Series)
(an ESP32-S3 dev board) instead of a real Raspberry Pi.

No signal remapping, no ICs, no passives: **2 connectors + 4 mounting holes**,
nothing else. See "Why no routed traces" below for the one place this
deviates from a literal wire-for-wire layout, and why.

## Why this works at all

1. **The T-ETH-Elite's 40-pin header already matches the standard Raspberry Pi
   GPIO header 1:1.** LilyGO's own official schematic
   (`schematic/T-ETH-ELite.pdf`, github.com/Xinyuan-LilyGO/LilyGO-T-ETH-Series)
   contains a table titled "Raspberry Pi PinOUT" (sourced from pinout.xyz)
   mapping all 40 physical header pins to the standard RPi GPIO function
   (SPI0 MOSI/MISO/SCLK/CE0/CE1, I2C, UART, PCM, EEPROM ID pins, 3V3/5V/GND).
   LilyGO wired their ESP32-S3's GPIOs internally so the header's external
   behavior is byte-for-byte a real Pi header. The TSN Lab HAT expects
   standard RPi SPI0 + GPIO25 (IRQ) + GPIO7 (nCE1) + a couple of GPIOs for a
   bus-node-ID selector — all present at the same pin numbers on the
   T-ETH-Elite. Net result: pin N on one side is pin N on the other, for
   N = 1..40, no exceptions.
2. **The mechanical layout is copied verbatim from KiCad's own official
   Raspberry-Pi-HAT project template**
   (`/usr/share/kicad/template/RaspberryPi-HAT/`), not re-derived:
   - 4 mounting holes, `MountingHole:MountingHole_2.7mm_M2.5` (2.7 mm
     drill, M2.5 clearance), at absolute (161.5, 47.5), (103.5, 96.5),
     (103.5, 47.5), (161.5, 96.5) mm — a 58 × 49 mm rectangle, which is the
     official HAT mounting-hole spec.
   - Board outline: the exact `gr_line`/`gr_arc` Edge.Cuts segments from the
     template, same absolute coordinates — the ~65 × 56.5 mm rounded-corner
     HAT shape, display-cable and camera-cable notches included, even though
     this board has no display/camera connectors of its own (harmless, and
     it means the outline is provably identical to the reference, not an
     approximation).
   - Header position: (103.5, 47.5) + (4.87, 1.27) = (108.37, 48.77) mm,
     exactly the template's own GPIO connector position.
3. **Net names are copied from the template's own net table**
   (`RaspberryPi-HAT.kicad_pcb`, nets 1–31), so the schematic's 40 labels
   read `+3V3`, `+5V`, `GND`, `GPIO2{slash}SDA1`, `ID_SDA`, etc. — the exact
   same strings the template uses (the `{slash}` is KiCad's own escaping of
   a literal `/` in a net name; it round-trips through KiCad correctly and
   is not something this project invented).

## Concentric header/socket placement

Per the design brief, **J1 (top, male, `PinHeader_2x20_P2.54mm_Vertical`,
F.Cu) and J2 (bottom, female, `PinSocket_2x20_P2.54mm_Vertical`, B.Cu) sit at
the exact same (x, y) anchor and rotation** — concentric, not offset. This was
verified empirically to be achievable cleanly: KiCad's `PinHeader_2x20` and
`PinSocket_2x20` footprint families are drawn as mirror images of each other
specifically so that a header mounted on the front and a socket mounted on
the back, at the same anchor, land every pin at the identical world (x, y).
Each of the 40 signals is therefore a **single shared plated through-hole**,
not two holes joined by copper.

**Real-world build note:** two independent, separately-sourced THT parts
cannot physically share one 1.0 mm drilled hole (whichever part solders in
first fills it). The buildable way to populate this design is a single
assembled **"2×20 GPIO stacking header"** per position — a widely-sold
Raspberry Pi HAT accessory that is a female socket on one face with long
pins protruding from the other, through the board, in one part. The BOM/CPL
still list two separate reference designators (J1 for the header/male role,
J2 for the socket/female role) because that is how the schematic/footprint
model the two mating faces; in practice one stacking-header part per
position satisfies both.

### Why no routed traces for most nets

Because J1 and J2 are coincident per pin, 36 of the 40 signals need no
copper at all — the coincident, same-net pads *are* the connection. Two
nets have more than one pin per connector and do need real copper:
- **+3V3** (pins 1 & 17): routed as a 3-segment dogleg that steps 3 mm off
  the pin row and back, because a naive straight pin-1-to-pin-17 trace would
  run directly across pins 3/5/7/9/11/13/15 sitting on that same line.
- **+5V** (pins 2 & 4): adjacent on the row, routed as one straight segment.

**GND** (8 pins per connector) is tied by a copper pour on both F.Cu and
B.Cu, per the design brief, rather than discrete traces.

### A KiCad quirk on pins 39/40, and how it's handled

Pins 39 and 40 of `Connector_Generic:Conn_02x20_Odd_Even` — confirmed via an
isolated minimal reproduction (a bare instance of the same library symbol
with nothing else in the sheet) — resolve, for **connectivity purposes
only**, to a y-coordinate mirrored across the pin row (anchor + 25.4 mm)
instead of the library's own documented anchor − 25.4 mm, while the pin
graphic still *draws* at the documented, correct position. Wiring pins
39/40 exactly like every other pin (which is what this schematic does, for
visual consistency with the other 38) therefore produces 6 ERC
false-positives ("Pin not connected" ×4, "Label not connected to anything"
×2) that do not reflect any real wiring defect — moving the wire/label to
match KiCad's buggy internal coordinate does clear the false-positive, but
only by drawing something that visually floats away from the real pin, which
would be more misleading than the false-positive it fixes. These 6 findings
are marked **Excluded** in the project (`kicad-cli sch erc` equivalent: `ERC
→ right-click → Exclude this violation`), persisted in
`t_eth_elite_hat_adapter.kicad_pro`, with this explanation on record.

Similarly, placing J1 and J2 concentrically means every pin position is a
"drilled holes co-located" condition — KiCad has a dedicated, warning-severity
rule for exactly this pattern (not an error), and it fires 40 times (one per
pin). This is the intended, deliberate design, not a defect.

## Deliverables in this folder

| File | What it is |
|---|---|
| `t_eth_elite_hat_adapter.kicad_pro/.kicad_sch/.kicad_pcb` | The KiCad 7 project |
| `gerbers/t_eth_elite_hat_adapter_gerbers.zip` | Fab-ready Gerbers (F.Cu, B.Cu, F/B.SilkS, F/B.Mask, Edge.Cuts) + Excellon drill + drill map + job file |
| `bom.csv` | 2 line items: the header (J1) and the socket (J2) |
| `cpl.csv` | Placement/position file (Designator, Mid X, Mid Y, Layer, Rotation) |
| `fp-lib-table` | Project-local footprint library table (MountingHole, Connector_PinHeader_2.54mm, Connector_PinSocket_2.54mm) so the project resolves its footprints standalone |

**Board spec:** 2-layer, 1.6 mm, 1 oz copper. Default net class: 0.3 mm
track, 0.2 mm clearance, 0.6 mm/0.3 mm via — comfortably inside JLCPCB's
"standard" 2-layer capability (this design never approaches fine-pitch
limits; the only routing is the two short +3V3/+5V segments described
above).

### ERC / DRC status

Tool note: the installed `kicad-cli` (7.0.11+dfsg-1build4, from this
machine's Ubuntu package) does not include the `sch erc` / `pcb drc`
subcommands (`kicad-cli sch erc` / `kicad-cli pcb drc` both return "Maximum
number of positional arguments exceeded" — the subcommand isn't registered
in this build, verified against `kicad-cli --help` at every level). Real ERC
was run through the actual KiCad 7.0.11 Eeschema GUI (Xvfb-hosted,
scripted); real DRC was cross-checked two ways: through the actual KiCad
7.0.11 Pcbnew GUI, and independently via `pcbnew.WriteDRCReport()` (the same
DRC engine, called directly through KiCad's own Python bindings) — both
agree.

- **ERC: 0 errors, 0 warnings, 6 exclusions** (the pins-39/40 false-positives
  above, individually excluded with reasons on record in the project file).
- **DRC: 0 errors, 40 warnings** (the "drilled holes co-located" findings
  above — all expected, all the same deliberate concentric-stacking pattern,
  none are courtyard, clearance, hole-clearance, or unconnected-item
  problems; those all read zero). Note: this KiCad build's "Ignore
  all"/"Exclude" actions for *DRC* (as opposed to *ERC*) did not persist to
  the project file in this environment (verified: a fresh reload shows the
  same 40 warnings again) — so unlike the ERC exclusions, don't expect the
  GUI to show these as already-excluded on first open; they are still
  correctly zero *errors*.

## Assembly

1. **4× M2.5 standoffs**, length TBD by the user — long enough to clear
   whatever sits on top of the T-ETH-Elite's own PCB underneath this
   adapter (not measured here, see caveats below) — through the 4 mounting
   holes.
2. **T-ETH-Elite**: friction-fit into the bottom (B.Cu) socket from below.
   There is no screw mounting to the T-ETH-Elite itself (see caveats) — it
   is held the same way any Arduino/Pi shield is normally held, by the
   header/socket pins alone.
3. **TSN Lab HAT**: sits on top, secured by its own screws down into this
   board's 4 holes (through its own oversized body — see caveats about the
   overhang).

## What is NOT verified — read this before ordering

- **The T-ETH-Elite's own board outline and mounting holes are not known
  precisely.** Its schematic (sheet 2) shows unpositioned mounting-hole
  reference designators (H2–H5) with no dimensioned drawing found. A public
  estimate puts the board around 50 × 67 mm, unverified. This adapter does
  **not** attempt to screw-mount the T-ETH-Elite — it relies purely on the
  friction fit of the 40-pin header/socket pair, the same way ordinary
  shields are normally held.
- **The TSN Lab HAT is itself an oversized, non-standard HAT**: its listed
  size is 57 × 75 × 23 mm versus the official 65 × 56.5 mm HAT envelope —
  about 19 mm longer. Only the 2 mounting holes nearest its 40-pin connector
  can be assumed to land on this adapter's standard hole positions; the far
  end of that HAT will overhang unsupported past this adapter board and may
  need the user's own added support/standoff underneath. This is not a
  verified mechanical fit for the whole HAT, just for the connector end.
- **Nothing here has been bench-tested.** No board, no HAT, and no
  T-ETH-Elite were in hand for this design — it is a design, not a
  verified-working assembly.
- **No order was placed.** There is no browser/checkout access, no payment
  method, no account available in this environment. The deliverable is
  fab-ready files for the user to upload themselves at jlcpcb.com — nothing
  here implies an order was placed.
