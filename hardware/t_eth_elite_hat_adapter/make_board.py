#!/usr/bin/env python3
"""
Generate t_eth_elite_hat_adapter.kicad_pcb from the numbers in GEOMETRY.md.

Run:  python3 make_board.py          (KiCad 7 pcbnew Python bindings required)

Why a generator and not a hand-drawn board: every coordinate here comes from
GEOMETRY.md, which was measured from LilyGo's own DXF/3D CAD.  A script lands
those numbers exactly, can be re-run when one of them changes, and diffs
legibly.  GEOMETRY.md is the source of truth; if this file and that file ever
disagree, that file is right.

--------------------------------------------------------------------------
COORDINATE FRAME  --  read this before touching any number below
--------------------------------------------------------------------------
GEOMETRY.md's frame ("G frame"): T-ETH-Elite top view, origin at the Elite's
PCB bottom-left corner, +x right, +y UP.  KiCad's PCB frame has +y DOWN.

The single conversion is kx()/ky()/V() below:

    kicad_x = ORIGIN_X + gx
    kicad_y = ORIGIN_Y - gy

i.e. the G frame origin (0, 0) sits at KiCad (100, 100) mm, and the board
occupies KiCad x 100.00..166.22, y 42.80..100.00.  Nothing else in this file
converts coordinates; every feature is specified in G-frame mm and passed
through V().  Angles: a G-frame CCW angle is a KiCad CW angle, which matters
only for the header footprint, and that is resolved empirically (see
place_header) rather than by reasoning about the sign.
--------------------------------------------------------------------------
"""

import csv
import json
import os
import re
import subprocess
import sys
import zipfile

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "t_eth_elite_hat_adapter"
PCB_PATH = os.path.join(HERE, NAME + ".kicad_pcb")
PRO_PATH = os.path.join(HERE, NAME + ".kicad_pro")

# ---------------------------------------------------------------- frame ----
ORIGIN_X = 100.0
ORIGIN_Y = 100.0


def kx(gx):
    """G-frame x -> KiCad x (mm)."""
    return ORIGIN_X + gx


def ky(gy):
    """G-frame y (+up) -> KiCad y (+down) (mm)."""
    return ORIGIN_Y - gy


def V(gx, gy):
    """G-frame point -> KiCad VECTOR2I (internal units)."""
    return pcbnew.VECTOR2I(pcbnew.FromMM(kx(gx)), pcbnew.FromMM(ky(gy)))


def MM(v):
    return pcbnew.FromMM(v)


def SZ(w, h):
    return pcbnew.VECTOR2I(pcbnew.FromMM(w), pcbnew.FromMM(h))


# ------------------------------------------------------- GEOMETRY.md data --
# Outline: 0..66.22 in x, 0..57.20 in y, 3 mm rounded corners.
BOARD_W = 66.22
BOARD_H = 57.20
CORNER_R = 3.0

# RJ45 clearance notch: rectangular bite out of the left edge.
NOTCH_X1, NOTCH_Y0, NOTCH_Y1 = 17.40, 10.00, 29.40

# 6 mounting holes, all Ø2.75, non-plated.
HOLE_D = 2.75
HOLES = [
    ("H1", 3.33, 4.63, "T-ETH-Elite bottom-left AND HAT near-left"),
    ("H2", 61.33, 4.63, "T-ETH-Elite bottom-right AND HAT near-right"),
    ("H3", 2.98, 46.23, "T-ETH-Elite top-left"),
    ("H4", 63.23, 46.20, "T-ETH-Elite top-right"),
    ("H5", 3.33, 53.63, "HAT far-left"),
    ("H6", 61.33, 53.63, "HAT far-right"),
]

# Tool-access holes for the Elite's two ST-1133 tactile switches, which this
# board would otherwise bury.  Centres are the midpoints of the switch
# footprints in LilyGo's CAD (x 41.48..45.98 and 52.10..56.60, y 45.85..49.35).
# Ø4.0 is a tweezer/pen tip, not a finger.
TOOL_D = 4.0
TOOL_HOLES = [
    ("H7", 43.73, 47.60, "BOOT", "tool access to the Elite's SW4 BOOT button"),
    ("H8", 54.35, 47.60, "RST", "tool access to the Elite's SW5 EN/RESET button"),
]

# 2x20 THT stacking header.  Pin grid from GEOMETRY.md.
HDR_X0 = 8.196
HDR_PITCH = 2.54
HDR_ROW_ODD = 3.360   # row nearer the board's y=0 edge
HDR_ROW_EVEN = 5.900
HDR_LIB = "/usr/share/kicad/footprints/Connector_PinHeader_2.54mm.pretty"
HDR_FP = "PinHeader_2x20_P2.54mm_Vertical"


def pin_xy(pin):
    """G-frame (x, y) of 40-pin header pin `pin` (1..40).

    Pin 1 is at the low-x end (nearest the RJ45 notch) on the row nearest the
    board's y=0 edge.  Odd pins on y=3.360, even pins on y=5.900.  See the
    README's "Which pin is pin 1" section for how this was established --
    GEOMETRY.md gives the grid but not the numbering.
    """
    k = (pin - 1) // 2
    return HDR_X0 + HDR_PITCH * k, (HDR_ROW_ODD if pin % 2 else HDR_ROW_EVEN)


# Standard Raspberry Pi 40-pin assignment.  (short silk label, table label)
RPI_PINS = {
    1: ("3V3", "3V3"),            2: ("5V", "5V"),
    3: ("SDA", "GPIO2 SDA1"),     4: ("5V", "5V"),
    5: ("SCL", "GPIO3 SCL1"),     6: ("GND", "GND"),
    7: ("GP4", "GPIO4"),          8: ("TXD", "GPIO14 TXD"),
    9: ("GND", "GND"),           10: ("RXD", "GPIO15 RXD"),
    11: ("GP17", "GPIO17"),      12: ("GP18", "GPIO18"),
    13: ("GP27", "GPIO27"),      14: ("GND", "GND"),
    15: ("GP22", "GPIO22"),      16: ("GP23", "GPIO23"),
    17: ("3V3", "3V3"),          18: ("GP24", "GPIO24"),
    19: ("MOSI", "GPIO10 MOSI"), 20: ("GND", "GND"),
    21: ("MISO", "GPIO9 MISO"),  22: ("GP25", "GPIO25"),
    23: ("SCLK", "GPIO11 SCLK"), 24: ("CE0", "GPIO8 CE0"),
    25: ("GND", "GND"),          26: ("CE1", "GPIO7 CE1"),
    27: ("IDSD", "GPIO0 ID_SD"), 28: ("IDSC", "GPIO1 ID_SC"),
    29: ("GP5", "GPIO5"),        30: ("GND", "GND"),
    31: ("GP6", "GPIO6"),        32: ("GP12", "GPIO12"),
    33: ("GP13", "GPIO13"),      34: ("GND", "GND"),
    35: ("GP19", "GPIO19"),      36: ("GP16", "GPIO16"),
    37: ("GP26", "GPIO26"),      38: ("GP20", "GPIO20"),
    39: ("GND", "GND"),          40: ("GP21", "GPIO21"),
}

EDGE_W = 0.10
SILK_W = 0.15


# ------------------------------------------------------------- primitives --
def add_seg(board, layer, p0, p1, width):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(V(*p0))
    s.SetEnd(V(*p1))
    s.SetLayer(layer)
    s.SetWidth(MM(width))
    board.Add(s)
    return s


def add_arc(board, layer, p0, pmid, p1, width):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_ARC)
    s.SetArcGeometry(V(*p0), V(*pmid), V(*p1))
    s.SetLayer(layer)
    s.SetWidth(MM(width))
    board.Add(s)
    return s


def add_poly(board, layer, pts, width, filled=True):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_POLY)
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for p in pts:
        chain.Append(V(*p))
    chain.SetClosed(True)
    poly = pcbnew.SHAPE_POLY_SET()
    poly.AddOutline(chain)
    s.SetPolyShape(poly)
    s.SetLayer(layer)
    s.SetWidth(MM(width))
    s.SetFilled(filled)
    board.Add(s)
    return s


def add_text(board, txt, gx, gy, h=1.0, w=None, th=0.15, angle=0,
             layer=None, just="center"):
    """Silkscreen text.  `angle` in degrees, CCW as seen in the G frame /
    looking down at the finished board (KiCad's own text-angle convention is
    the same way round, so no sign flip is needed here)."""
    if w is None:
        w = h * 0.82
    if layer is None:
        layer = pcbnew.F_SilkS
    t = pcbnew.PCB_TEXT(board)
    t.SetText(txt)
    t.SetLayer(layer)
    t.SetPosition(V(gx, gy))
    t.SetTextSize(SZ(w, h))
    t.SetTextThickness(MM(th))
    t.SetTextAngle(pcbnew.EDA_ANGLE(angle, pcbnew.DEGREES_T))
    t.SetHorizJustify({
        "center": pcbnew.GR_TEXT_H_ALIGN_CENTER,
        "left": pcbnew.GR_TEXT_H_ALIGN_LEFT,
        "right": pcbnew.GR_TEXT_H_ALIGN_RIGHT,
    }[just])
    t.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
    board.Add(t)
    return t


def add_npth(board, ref, gx, gy, diameter, descr):
    """A non-plated round hole, as a one-pad footprint so it exports to the
    drill file and is excluded from BOM/position files."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetFPID(pcbnew.LIB_ID("t_eth_elite_hat_adapter",
                             "NPTH_%.2fmm" % diameter))
    fp.SetReference(ref)
    fp.SetValue("NPTH_%.2fmm" % diameter)
    fp.SetDescription(descr)
    fp.Reference().SetVisible(False)
    fp.Value().SetVisible(False)
    fp.SetAttributes(pcbnew.FP_EXCLUDE_FROM_POS_FILES |
                     pcbnew.FP_EXCLUDE_FROM_BOM)

    pad = pcbnew.PAD(fp)
    pad.SetNumber("")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_CIRCLE)
    pad.SetSize(SZ(diameter, diameter))
    pad.SetDrillSize(SZ(diameter, diameter))
    pad.SetLayerSet(pcbnew.PAD.UnplatedHoleMask())
    pad.SetPos0(pcbnew.VECTOR2I(0, 0))
    fp.Add(pad)

    board.Add(fp)
    fp.SetPosition(V(gx, gy))
    pad.SetPosition(V(gx, gy))
    pad.SetPos0(pcbnew.VECTOR2I(0, 0))
    return fp


# ---------------------------------------------------------------- outline --
def draw_outline(board):
    L, R, B, T = 0.0, BOARD_W, 0.0, BOARD_H
    r = CORNER_R
    d = r * (1 - 0.70710678)  # arc midpoint inset for a 90 deg corner

    # bottom edge, between the two bottom corner arcs
    add_seg(board, pcbnew.Edge_Cuts, (L + r, B), (R - r, B), EDGE_W)
    # bottom-right corner
    add_arc(board, pcbnew.Edge_Cuts, (R - r, B), (R - d, B + d), (R, B + r), EDGE_W)
    # right edge
    add_seg(board, pcbnew.Edge_Cuts, (R, B + r), (R, T - r), EDGE_W)
    # top-right corner
    add_arc(board, pcbnew.Edge_Cuts, (R, T - r), (R - d, T - d), (R - r, T), EDGE_W)
    # top edge
    add_seg(board, pcbnew.Edge_Cuts, (R - r, T), (L + r, T), EDGE_W)
    # top-left corner
    add_arc(board, pcbnew.Edge_Cuts, (L + r, T), (L + d, T - d), (L, T - r), EDGE_W)
    # left edge, upper part, down to the notch
    add_seg(board, pcbnew.Edge_Cuts, (L, T - r), (L, NOTCH_Y1), EDGE_W)
    # the RJ45 notch: three sides of a rectangular bite out of the left edge
    add_seg(board, pcbnew.Edge_Cuts, (L, NOTCH_Y1), (NOTCH_X1, NOTCH_Y1), EDGE_W)
    add_seg(board, pcbnew.Edge_Cuts, (NOTCH_X1, NOTCH_Y1), (NOTCH_X1, NOTCH_Y0), EDGE_W)
    add_seg(board, pcbnew.Edge_Cuts, (NOTCH_X1, NOTCH_Y0), (L, NOTCH_Y0), EDGE_W)
    # left edge, lower part
    add_seg(board, pcbnew.Edge_Cuts, (L, NOTCH_Y0), (L, B + r), EDGE_W)
    # bottom-left corner
    add_arc(board, pcbnew.Edge_Cuts, (L, B + r), (L + d, B + d), (L + r, B), EDGE_W)


# ----------------------------------------------------------------- header --
def place_header(board):
    """Place one 2x20 vertical pin header so its pads land on GEOMETRY.md's
    grid.

    The stock footprint's local frame has pad 1 at (0,0), pad 2 at (+2.54, 0)
    and pad 3 at (0, +2.54) -- i.e. its two *columns* run along local x and its
    twenty *rows* along local y, which is the transpose of what this board
    needs.  Rather than trusting a sign convention, we try each 90 deg
    orientation, measure the resulting pad-to-pad vectors in board coordinates,
    and keep the one where pad3-pad1 is (+2.54, 0) and pad2-pad1 is (0, -2.54)
    in KiCad coordinates -- i.e. increasing pin pair k runs along +x, and the
    even pins sit 2.54 mm *above* the odd pins in the G frame (KiCad -y),
    which is GEOMETRY.md's rows 3.360 (odd) and 5.900 (even).  Then translate
    so pad 1 lands on pin 1.
    """
    want_p3 = (MM(HDR_PITCH), 0)
    want_p2 = (0, -MM(HDR_PITCH))
    tol = MM(0.001)

    chosen = None
    for deg in (0, 90, 180, 270):
        fp = pcbnew.FootprintLoad(HDR_LIB, HDR_FP)
        if fp is None:
            raise RuntimeError("cannot load %s from %s" % (HDR_FP, HDR_LIB))
        fp.SetPosition(pcbnew.VECTOR2I(0, 0))
        fp.SetOrientation(pcbnew.EDA_ANGLE(deg, pcbnew.DEGREES_T))
        p = {pad.GetNumber(): pad.GetPosition() for pad in fp.Pads()}
        d3 = (p["3"].x - p["1"].x, p["3"].y - p["1"].y)
        d2 = (p["2"].x - p["1"].x, p["2"].y - p["1"].y)
        ok = (abs(d3[0] - want_p3[0]) < tol and abs(d3[1] - want_p3[1]) < tol and
              abs(d2[0] - want_p2[0]) < tol and abs(d2[1] - want_p2[1]) < tol)
        print("  orientation %3d deg: pad3-pad1=(%+.3f,%+.3f) pad2-pad1=(%+.3f,%+.3f) %s"
              % (deg, pcbnew.ToMM(d3[0]), pcbnew.ToMM(d3[1]),
                 pcbnew.ToMM(d2[0]), pcbnew.ToMM(d2[1]),
                 "<-- use this" if ok else ""))
        if ok and chosen is None:
            chosen = deg
    if chosen is None:
        raise RuntimeError("no 90-degree orientation gives the required pad grid")

    fp = pcbnew.FootprintLoad(HDR_LIB, HDR_FP)
    fp.SetOrientation(pcbnew.EDA_ANGLE(chosen, pcbnew.DEGREES_T))
    fp.SetFPID(pcbnew.LIB_ID("Connector_PinHeader_2.54mm", HDR_FP))
    fp.SetReference("J1")
    fp.SetValue("2x20 stacking header")
    fp.Value().SetVisible(False)
    fp.Reference().SetVisible(True)
    fp.Reference().SetPosition(V(61.5, 12.0))
    fp.Reference().SetTextSize(SZ(0.85, 1.0))
    fp.Reference().SetTextThickness(MM(0.15))
    fp.Reference().SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
    fp.SetAttributes(pcbnew.FP_THROUGH_HOLE)
    board.Add(fp)

    # translate so pad "1" sits exactly on pin 1
    pad1 = {pad.GetNumber(): pad for pad in fp.Pads()}["1"]
    target = V(*pin_xy(1))
    cur = pad1.GetPosition()
    pos = fp.GetPosition()
    fp.SetPosition(pcbnew.VECTOR2I(pos.x + (target.x - cur.x),
                                   pos.y + (target.y - cur.y)))
    return fp


# ------------------------------------------------------------- silkscreen --
def draw_silk(board):
    cx = BOARD_W / 2.0

    # --- what the board is, and which way round ---------------------------
    add_text(board, "T-ETH-Elite  ->  RPi HAT adapter", cx, 55.85,
             h=1.8, w=1.5, th=0.28)
    add_text(board, "ELITE BELOW / HAT ABOVE - pin N = pin N", 22.0, 49.0,
             h=1.0, th=0.15)

    # --- RJ45 notch -------------------------------------------------------
    add_text(board, "RJ45 side", 9.5, 31.6, h=1.3, w=1.1, th=0.2)
    # a little arrow pointing down into the notch
    add_seg(board, pcbnew.F_SilkS, (9.5, 30.5), (9.5, 29.8), SILK_W)
    add_seg(board, pcbnew.F_SilkS, (9.5, 29.8), (8.9, 30.4), SILK_W)
    add_seg(board, pcbnew.F_SilkS, (9.5, 29.8), (10.1, 30.4), SILK_W)

    # --- pin 1 marker -----------------------------------------------------
    px1, _ = pin_xy(1)
    add_poly(board, pcbnew.F_SilkS,
             [(px1, 8.70), (px1 - 0.70, 9.65), (px1 + 0.70, 9.65)],
             0.01, filled=True)
    add_text(board, "PIN 1", 12.9, 9.20, h=0.9, th=0.15)

    # --- mounting-hole roles ---------------------------------------------
    add_text(board, "BOTH", 3.40, 8.30, h=1.0, th=0.15)
    add_text(board, "BOTH", 61.30, 8.30, h=1.0, th=0.15)
    add_text(board, "ELITE", 9.20, 46.23, h=1.1, th=0.16)
    add_text(board, "ELITE", 62.80, 42.80, h=1.1, th=0.16)
    add_text(board, "HAT", 9.00, 53.63, h=1.1, th=0.16)
    add_text(board, "HAT", 58.50, 53.63, h=1.1, th=0.16)

    # --- tool-access holes -----------------------------------------------
    for _ref, hx, _hy, label, _d in TOOL_HOLES:
        add_text(board, label, hx, 52.60, h=1.1, th=0.16)

    # --- per-pin labels ---------------------------------------------------
    # Each label is a vertical string reading up from just above the header
    # body (the connector's plastic covers y 0.86..8.40, so nothing printed
    # below ~8.6 would be visible).  Columns whose label column would fall
    # inside the RJ45 notch's x span, allowing for glyph width, are skipped:
    # those pins get their names in the table instead.  Empirically that is
    # k=0..4, i.e. pins 1-10.
    label_x_min = NOTCH_X1 + 0.5 + 0.635
    skipped = []
    for pin in range(1, 41):
        gx, _gy = pin_xy(pin)
        if gx < label_x_min:
            skipped.append(pin)
            continue
        short = RPI_PINS[pin][0]
        xoff = -0.635 if pin % 2 else 0.635
        add_text(board, "%d %s" % (pin, short), gx + xoff, 8.85,
                 h=0.8, w=0.65, th=0.12, angle=90, just="left")

    add_text(board, "ODD = EDGE ROW.  PINS %d-%d LABELLED IN TABLE ONLY."
             % (skipped[0], skipped[-1]),
             38.0, 15.9, h=0.85, w=0.7, th=0.13)

    # --- back side: say which way up the board goes, from below too -------
    for txt, gy, h in (("T-ETH-Elite SIDE", 22.4, 2.0),
                       ("stacking-header socket faces down", 19.4, 1.1)):
        t = add_text(board, txt, 38.0, gy, h=h, th=h * 0.16,
                     layer=pcbnew.B_SilkS)
        t.SetMirrored(True)

    # --- full 40-pin table ------------------------------------------------
    add_text(board, "ODD PINS", 27.0, 44.2, h=1.0, th=0.16)
    add_text(board, "EVEN PINS", 44.0, 44.2, h=1.0, th=0.16)
    y = 42.60
    for i in range(20):
        odd, even = 2 * i + 1, 2 * i + 2
        add_text(board, "%d %s" % (odd, RPI_PINS[odd][1]), 27.0, y,
                 h=1.0, w=0.85, th=0.15)
        add_text(board, "%s %d" % (RPI_PINS[even][1], even), 44.0, y,
                 h=1.0, w=0.85, th=0.15)
        y -= 1.34


# ------------------------------------------------------------------ build --
def build():
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(2)
    bds = board.GetDesignSettings()
    bds.SetBoardThickness(MM(1.6))
    # Put the drill/place file origin on the G frame's own origin, so the
    # exported drill and position files read in GEOMETRY.md's coordinates
    # (export everything with --use-drill-file-origin to keep them aligned).
    bds.SetAuxOrigin(V(0.0, 0.0))

    draw_outline(board)
    for ref, gx, gy, descr in HOLES:
        add_npth(board, ref, gx, gy, HOLE_D, descr)
    for ref, gx, gy, _label, descr in TOOL_HOLES:
        add_npth(board, ref, gx, gy, TOOL_D, descr)
    print("placing the 2x20 header:")
    place_header(board)
    draw_silk(board)

    pcbnew.SaveBoard(PCB_PATH, board)
    print("wrote %s" % PCB_PATH)
    return board


def write_project():
    """Minimal KiCad 7 project file.  KiCad fills in everything it needs."""
    pro = {
        "board": {"design_settings": {"defaults": {}}},
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": NAME + ".kicad_pro", "version": 1},
        "net_settings": {
            "classes": [{
                "bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25,
                "diff_pair_via_gap": 0.25, "diff_pair_width": 0.2,
                "line_style": 0, "microvia_diameter": 0.3,
                "microvia_drill": 0.1, "name": "Default", "pcb_color": "rgba(0, 0, 0, 0.000)",
                "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": 0.3,
                "via_diameter": 0.6, "via_drill": 0.3, "wire_width": 6,
            }],
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
        "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
        "sheets": [],
        "text_variables": {},
    }
    with open(PRO_PATH, "w") as f:
        json.dump(pro, f, indent=2)
        f.write("\n")
    print("wrote %s" % PRO_PATH)


# ------------------------------------------------------------------ check --
def verify():
    """Read the saved .kicad_pcb back and compare every hole and pad centre
    against GEOMETRY.md.  Fails loudly; the generator gets fixed, not this."""
    board = pcbnew.LoadBoard(PCB_PATH)
    tol = 0.01
    rows = []
    worst = 0.0
    ok = True

    holes = {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref.startswith("H"):
            pad = list(fp.Pads())[0]
            p = pad.GetPosition()
            holes[ref] = (pcbnew.ToMM(p.x) - ORIGIN_X,
                          ORIGIN_Y - pcbnew.ToMM(p.y),
                          pcbnew.ToMM(pad.GetDrillSize().x))

    spec_holes = ([(r, x, y, HOLE_D) for r, x, y, _ in HOLES] +
                  [(r, x, y, TOOL_D) for r, x, y, _l, _d in TOOL_HOLES])
    for ref, sx, sy, sd in spec_holes:
        ax, ay, ad = holes[ref]
        dx, dy = ax - sx, ay - sy
        err = max(abs(dx), abs(dy), abs(ad - sd))
        worst = max(worst, err)
        good = err <= tol
        ok = ok and good
        rows.append((ref, "%.3f, %.3f  D%.2f" % (sx, sy, sd),
                     "%.3f, %.3f  D%.2f" % (ax, ay, ad),
                     "%+.4f %+.4f" % (dx, dy), "OK" if good else "FAIL"))

    j1 = [fp for fp in board.GetFootprints() if fp.GetReference() == "J1"][0]
    pads = {p.GetNumber(): p for p in j1.Pads()}
    for pin in range(1, 41):
        sx, sy = pin_xy(pin)
        p = pads[str(pin)].GetPosition()
        ax = pcbnew.ToMM(p.x) - ORIGIN_X
        ay = ORIGIN_Y - pcbnew.ToMM(p.y)
        dx, dy = ax - sx, ay - sy
        err = max(abs(dx), abs(dy))
        worst = max(worst, err)
        good = err <= tol
        ok = ok and good
        rows.append(("J1.%d" % pin, "%.3f, %.3f" % (sx, sy),
                     "%.3f, %.3f" % (ax, ay),
                     "%+.4f %+.4f" % (dx, dy), "OK" if good else "FAIL"))

    w = [max(len(str(r[i])) for r in rows) for i in range(5)]
    hdr = ("ref", "GEOMETRY.md (mm)", "in .kicad_pcb (mm)", "delta", "")
    w = [max(w[i], len(hdr[i])) for i in range(5)]
    line = "  ".join("-" * x for x in w)
    print(line)
    print("  ".join(hdr[i].ljust(w[i]) for i in range(5)))
    print(line)
    for r in rows:
        print("  ".join(str(r[i]).ljust(w[i]) for i in range(5)))
    print(line)
    print("worst error: %.5f mm   tolerance: %.3f mm   %s"
          % (worst, tol, "ALL OK" if ok else "FAILURES PRESENT"))

    pad0 = list(pads.values())[0]
    print("header pad: hole D%.3f mm, pad D%.3f mm, %s, layers both sides: %s"
          % (pcbnew.ToMM(pad0.GetDrillSize().x), pcbnew.ToMM(pad0.GetSize().x),
             "PTH" if pad0.GetAttribute() == pcbnew.PAD_ATTRIB_PTH else "?",
             pad0.IsOnLayer(pcbnew.F_Cu) and pad0.IsOnLayer(pcbnew.B_Cu)))
    print("tracks: %d   zones: %d   nets (incl. no-net): %d"
          % (len(board.GetTracks()), board.GetAreaCount(),
             board.GetNetCount()))
    return ok, board


def run_drc(board):
    """This machine's kicad-cli has no `pcb drc` subcommand; call the same DRC
    engine through the Python bindings instead.  Whatever it says, it says."""
    # Point KiCad at its own installed footprint library so the
    # lib_footprint_issues test can actually compare J1 against the stock
    # PinHeader_2x20 footprint.  Without this the headless bindings cannot
    # resolve any library and the test degrades to "library not configured".
    # This makes the check stronger, not weaker.  The six Ø2.75 and two Ø4.0
    # NPTH holes are generated inline and belong to no library at all, so
    # they still report; that finding is left in, not excluded.
    os.environ.setdefault("KICAD7_FOOTPRINT_DIR", "/usr/share/kicad/footprints")
    rpt = os.path.join(HERE, "drc.rpt")
    units = getattr(pcbnew, "EDA_UNITS_MILLIMETRES", None)
    if units is None:
        units = getattr(pcbnew, "EDA_UNITS_MM", 1)
    okay = pcbnew.WriteDRCReport(board, rpt, units, True)
    print("WriteDRCReport -> %s (%s)" % (okay, rpt))
    with open(rpt) as f:
        print(f.read())


def write_bom_cpl():
    with open(os.path.join(HERE, "bom.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["Comment", "Designator", "Footprint", "Quantity",
                     "Description", "LCSC"])
        wr.writerow([
            "2x20 2.54mm stacking header", "J1",
            "Connector_PinHeader_2.54mm:PinHeader_2x20_P2.54mm_Vertical", 1,
            "40-way (2x20) 2.54 mm stacking header: female socket barrel "
            "~13 mm below the PCB, pins ~9 mm above it, one part through one "
            "set of 40 holes. Same class of part LilyGO fit to their own "
            "T-ETH-Elite shields.", ""])
    print("wrote bom.csv")


def export_all():
    """kicad-cli exports.  Everything is plotted against the drill/place file
    origin, which build() put on the G frame origin -- so the drill files and
    cpl.csv read directly in GEOMETRY.md coordinates and can be diffed against
    it by eye."""
    gerb = os.path.join(HERE, "gerbers")
    if os.path.isdir(gerb):
        for f in os.listdir(gerb):
            os.remove(os.path.join(gerb, f))
    else:
        os.mkdir(gerb)

    def cli(*args):
        subprocess.run(["kicad-cli", "pcb", "export"] + list(args) + [PCB_PATH],
                       check=True, cwd=HERE, stdout=subprocess.DEVNULL)

    cli("gerbers", "-o", "gerbers/", "--use-drill-file-origin",
        "--layers", "F.Cu,B.Cu,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts")
    cli("drill", "-o", "gerbers/", "--format", "excellon",
        "--drill-origin", "plot", "--excellon-separate-th",
        "--generate-map", "--map-format", "gerberx2")
    cli("pos", "-o", "cpl.csv", "--format", "csv", "--units", "mm",
        "--side", "both", "--use-drill-file-origin")
    cli("svg", "-o", "preview.svg", "--page-size-mode", "2",
        "--exclude-drawing-sheet", "--drill-shape-opt", "2",
        "--layers", "Edge.Cuts,F.Cu,F.Silkscreen,F.Mask")

    # kicad-cli's "board area only" page mode still emits a page-sized canvas,
    # so retarget the viewBox onto the board plus a 1.5 mm margin.
    svg = os.path.join(HERE, "preview.svg")
    s = open(svg).read()
    s = re.sub(r'width="[^"]+mm" height="[^"]+mm" viewBox="[^"]+"',
               'width="%.2fmm" height="%.2fmm" viewBox="-1.5 -1.5 %.2f %.2f"'
               % (BOARD_W + 3, BOARD_H + 3, BOARD_W + 3, BOARD_H + 3),
               s, count=1)
    open(svg, "w").write(s)
    subprocess.run(["rsvg-convert", "-w", "1500", "-b", "#101418",
                    "preview.svg", "-o", "preview.png"], check=True, cwd=HERE)

    # kicad-cli drops a local-state .kicad_prl next to the board; it is not a
    # deliverable and is not committed.
    prl = os.path.join(HERE, NAME + ".kicad_prl")
    if os.path.exists(prl):
        os.remove(prl)

    zf = os.path.join(gerb, NAME + "_gerbers.zip")
    with zipfile.ZipFile(zf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(gerb)):
            if f.endswith(".zip"):
                continue
            z.write(os.path.join(gerb, f), f)
    print("wrote gerbers/, cpl.csv, preview.svg, preview.png, %s"
          % os.path.basename(zf))


def main():
    board = build()
    write_project()
    print()
    ok, board = verify()
    print()
    run_drc(board)
    write_bom_cpl()
    export_all()
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
