#!/usr/bin/env python3
"""Generates the ratatouille KiCad schematic plus a PNG preview.

Writes ratatouille.kicad_sch in the KiCad 7 format, so it opens in KiCad 7, 8 and 9. It also writes
a minimal ratatouille.kicad_pro and preview.png (rendered by ImageMagick from the same geometry).
Every symbol is embedded in the file, so no extra libraries are needed. KiCad will warn that the
"ratatouille" library is missing; that's expected and harmless.

Run: python3 gen_kicad.py   (then open ratatouille.kicad_pro in KiCad)
It also prints the netlist and fails if any pin is left unconnected.
"""
import collections, json, math, os, subprocess, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = "ratatouille"
NS = uuid.UUID("6f1c2a40-3b1e-4d2a-9a51-2b7f0c9e1d11")
_n = 0


def uid():
    global _n
    _n += 1
    return str(uuid.uuid5(NS, str(_n)))


ROOT = uid()
POWER_NETS = {"+5V": "up", "+3V3": "up", "GND": "down"}


def q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def num(v):
    v = round(v, 4) + 0.0
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def font(size=1.27, hide=False, justify=None, bold=False):
    b = " bold" if bold else ""
    j = f" (justify {justify})" if justify else ""
    h = " hide" if hide else ""
    return f"(effects (font (size {num(size)} {num(size)}){b}){j}{h})"


def on_grid(x, y):
    for v in (x, y):
        assert abs(v / 1.27 - round(v / 1.27)) < 1e-6, f"off-grid point {x},{y}"


# ---------------------------------------------------------------- symbol library
class Sym:
    def __init__(self, name, ref, value, power=False, hide_names=False):
        self.name, self.ref, self.value = name, ref, value
        self.power, self.hide_names = power, hide_names
        self.gfx = []      # ('rect',x1,y1,x2,y2,fill) ('poly',pts,width,fill) ('circle',cx,cy,r,fill) ('text',s,x,y,size)
        self.pins = []     # dict(num,name,type,x,y,ang,len,hidden)
        self.ref_at = (0, 0, "")     # lib coords + justify
        self.val_at = (0, 0, "")

    def pin(self, number, name, ptype, x, y, ang, length, hidden=False):
        self.pins.append(dict(num=number, name=name, type=ptype, x=x, y=y, ang=ang, len=length, hidden=hidden))

    def sexpr(self):
        n = self.name
        flags = "(power) " if self.power else ""
        names = "(pin_names (offset 0) hide)" if self.hide_names or self.power else "(pin_names (offset 1.016))"
        out = [f'    (symbol {q("ratatouille:" + n)} {flags}(pin_numbers hide) {names} (in_bom yes) (on_board yes)']
        rx, ry, rj = self.ref_at
        vx, vy, vj = self.val_at
        out.append(f'      (property "Reference" {q(self.ref)} (at {num(rx)} {num(ry)} 0) {font(hide=self.power, justify=rj or None)})')
        out.append(f'      (property "Value" {q(self.value)} (at {num(vx)} {num(vy)} 0) {font(justify=vj or None)})')
        out.append(f'      (property "Footprint" "" (at 0 0 0) {font(hide=True)})')
        out.append(f'      (property "Datasheet" "" (at 0 0 0) {font(hide=True)})')
        out.append(f'      (symbol {q(n + "_0_1")}')
        for g in self.gfx:
            if g[0] == "rect":
                _, x1, y1, x2, y2, fill = g
                out.append(f"        (rectangle (start {num(x1)} {num(y1)}) (end {num(x2)} {num(y2)}) (stroke (width 0.254) (type default)) (fill (type {fill})))")
            elif g[0] == "poly":
                _, pts, w, fill = g
                p = " ".join(f"(xy {num(x)} {num(y)})" for x, y in pts)
                out.append(f"        (polyline (pts {p}) (stroke (width {num(w)}) (type default)) (fill (type {fill})))")
            elif g[0] == "circle":
                _, cx, cy, r, fill = g
                out.append(f"        (circle (center {num(cx)} {num(cy)}) (radius {num(r)}) (stroke (width 0.254) (type default)) (fill (type {fill})))")
            elif g[0] == "text":
                _, s, x, y, size = g
                out.append(f"        (text {q(s)} (at {num(x)} {num(y)} 0) {font(size)})")
        out.append("      )")
        out.append(f'      (symbol {q(n + "_1_1")}')
        for p in self.pins:
            h = " hide" if p["hidden"] else ""
            out.append(f'        (pin {p["type"]} line (at {num(p["x"])} {num(p["y"])} {p["ang"]}) (length {num(p["len"])}){h}'
                       f' (name {q(p["name"])} {font()}) (number {q(p["num"])} {font()}))')
        out.append("      )")
        out.append("    )")
        return "\n".join(out)


def module(name, ref, value, left, right, half):
    """Box symbol. left/right: list of (pin name, type, number) or None for a gap."""
    s = Sym(name, ref, value)
    n = max(len(left), len(right))
    top = math.ceil((n - 1) / 2) * 2.54
    bot = top - (n - 1) * 2.54
    s.gfx.append(("rect", -half, top + 2.54, half, bot - 2.54, "background"))
    for side, pins in (("L", left), ("R", right)):
        for i, p in enumerate(pins):
            if p is None:
                continue
            pname, ptype, pnum = p
            y = top - i * 2.54
            if side == "L":
                s.pin(pnum, pname, ptype, -half - 2.54, y, 0, 2.54)
            else:
                s.pin(pnum, pname, ptype, half + 2.54, y, 180, 2.54)
    s.ref_at = (0, top + 2.54 + 1.27, "bottom")
    s.val_at = (0, bot - 2.54 - 1.27, "top")
    return s


def resistor():
    s = Sym("R", "R", "R", hide_names=True)
    s.gfx.append(("rect", -1.016, -2.54, 1.016, 2.54, "none"))
    s.pin("1", "~", "passive", 0, 3.81, 270, 1.27)
    s.pin("2", "~", "passive", 0, -3.81, 90, 1.27)
    s.ref_at, s.val_at = (2.032, 1.27, "left"), (2.032, -1.27, "left")
    return s


def cap():
    s = Sym("C", "C", "C", hide_names=True)
    s.gfx.append(("poly", [(-2.032, 0.762), (2.032, 0.762)], 0.508, "none"))
    s.gfx.append(("poly", [(-2.032, -0.762), (2.032, -0.762)], 0.508, "none"))
    s.pin("1", "~", "passive", 0, 3.81, 270, 3.048)
    s.pin("2", "~", "passive", 0, -3.81, 90, 3.048)
    s.ref_at, s.val_at = (2.794, 1.27, "left"), (2.794, -1.27, "left")
    return s


def cap_pol():
    s = Sym("C_Polarized", "C", "C_Polarized", hide_names=True)
    s.gfx.append(("rect", -2.286, 0.508, 2.286, 1.016, "none"))
    s.gfx.append(("rect", -2.286, -0.508, 2.286, -1.016, "outline"))
    s.gfx.append(("poly", [(-2.159, 2.032), (-1.143, 2.032)], 0.2, "none"))
    s.gfx.append(("poly", [(-1.651, 2.54), (-1.651, 1.524)], 0.2, "none"))
    s.pin("1", "+", "passive", 0, 3.81, 270, 2.794)
    s.pin("2", "-", "passive", 0, -3.81, 90, 2.794)
    s.ref_at, s.val_at = (3.048, 1.27, "left"), (3.048, -1.27, "left")
    return s


def npn():
    s = Sym("Q_NPN_EBC", "Q", "2N2222A", hide_names=True)
    s.gfx.append(("circle", 1.27, 0, 2.8448, "none"))
    s.gfx.append(("poly", [(0.635, 1.905), (0.635, -1.905)], 0.508, "none"))
    s.gfx.append(("poly", [(0.635, 0.635), (2.54, 2.54)], 0.254, "none"))
    s.gfx.append(("poly", [(0.635, -0.635), (2.54, -2.54)], 0.254, "none"))
    s.gfx.append(("poly", [(2.286, -2.286), (1.397, -2.032), (2.032, -1.397), (2.286, -2.286)], 0.1, "outline"))
    s.gfx.append(("text", "B", -1.524, 0.889, 1.0))
    s.gfx.append(("text", "C", 3.429, 3.81, 1.0))
    s.gfx.append(("text", "E", 3.429, -3.81, 1.0))
    s.pin("1", "E", "passive", 2.54, -5.08, 90, 2.54)
    s.pin("2", "B", "input", -2.54, 0, 0, 3.175)
    s.pin("3", "C", "passive", 2.54, 5.08, 270, 2.54)
    s.ref_at, s.val_at = (5.08, 1.27, "left"), (5.08, -1.27, "left")
    return s


def power(net, direction):
    s = Sym(net.replace("+", "P"), "#PWR", net, power=True)
    if direction == "up":
        s.gfx.append(("poly", [(-0.762, 1.27), (0, 2.54), (0.762, 1.27)], 0, "none"))
        s.gfx.append(("poly", [(0, 0), (0, 2.54)], 0, "none"))
        s.pin("1", net, "power_in", 0, 0, 90, 0, hidden=True)
        s.val_at = (0, 3.556, "")
    else:
        s.gfx.append(("poly", [(0, 0), (0, -1.27), (1.27, -1.27), (0, -2.54), (-1.27, -1.27), (0, -1.27)], 0, "none"))
        s.pin("1", net, "power_in", 0, 0, 270, 0, hidden=True)
        s.val_at = (0, -3.81, "")
    s.ref_at = (0, 0, "")
    return s


LIB = {}
for sym in [
    module("USB_C_6P", "J", "USB-C 6-pin breakout",
           [("CC1", "no_connect", "3"), ("CC2", "no_connect", "4"), ("D+", "no_connect", "5"), ("D-", "no_connect", "6")],
           [("VBUS", "power_out", "1"), ("GND", "power_out", "2")], 7.62),
    module("Jumper2", "JP", "Jumper", [("~", "passive", "1")], [("~", "passive", "2")], 3.81),
    module("NiMH_In", "J", "NiMH in", [], [("+", "passive", "1"), ("-", "passive", "2")], 7.62),
    module("ESP32_CAM", "U", "ESP32 CAM", [("5V", "power_in", "1"), ("GND", "power_in", "2")], [], 10.16),
    module("ESP32S3_DevKit", "U", "ESP32-S3", [("5V", "power_in", "1")] + [None] * 8 + [("GND", "power_in", "2")],
           [("3V3", "power_out", "3"), ("GPIO4", "output", "4"), ("GPIO5", "output", "5"), ("GPIO6", "output", "6"),
            ("GPIO7 BL", "output", "7"), ("GPIO8 RST", "output", "8"), ("GPIO9 DC", "output", "9"),
            ("GPIO10 CS", "output", "10"), ("GPIO11 MOSI", "output", "11"), ("GPIO12 SCK", "output", "12")], 10.16),
    module("Servo", "J", "Servo", [("SIG (orange)", "input", "3"), ("V+ (red)", "passive", "2"), ("GND (brown)", "passive", "1")], [], 10.16),
    module("KY008", "J", "KY-008", [("S", "passive", "1"), ("mid (NC)", "no_connect", "2"), ("-", "passive", "3")], [], 7.62),
    module("TFT_ST7789", "J", "TFT", [("VCC", "power_in", "2"), ("SCL", "input", "3"), ("SDA", "input", "4"), ("RES", "input", "5"),
                                       ("DC", "input", "6"), ("CS", "input", "7"), ("BLK", "input", "8"), ("GND", "power_in", "1")], [], 7.62),
    resistor(), cap(), cap_pol(), npn(),
    power("+5V", "up"), power("+3V3", "up"), power("GND", "down"),
]:
    LIB[sym.name] = sym


# ---------------------------------------------------------------- schematic items
parts, wires, labels, junctions, noconns, texts = [], [], [], [], [], []
_pwr = [0]


class Part:
    def __init__(self, sym, ref, value, x, y, footprint="", fields=None):
        on_grid(x, y)
        self.sym, self.ref, self.value, self.x, self.y = sym, ref, value, x, y
        self.footprint = footprint
        self.uuid = uid()
        self.pin_uuids = {p["num"]: uid() for p in sym.pins}
        parts.append(self)

    def pin(self, key):
        for p in self.sym.pins:
            if key in (p["num"], p["name"]):
                return p
        raise KeyError(f"{self.ref}: no pin {key}")

    def end(self, key):
        p = self.pin(key)
        return (self.x + p["x"], self.y - p["y"])

    def sexpr(self):
        s = self.sym
        rx, ry, rj = s.ref_at
        vx, vy, vj = s.val_at
        o = [f'  (symbol (lib_id {q("ratatouille:" + s.name)}) (at {num(self.x)} {num(self.y)} 0) (unit 1)',
             f'    (in_bom yes) (on_board yes) (dnp no) (uuid {q(self.uuid)})',
             f'    (property "Reference" {q(self.ref)} (at {num(self.x + rx)} {num(self.y - ry)} 0) {font(hide=s.power, justify=rj or None)})',
             f'    (property "Value" {q(self.value)} (at {num(self.x + vx)} {num(self.y - vy)} 0) {font(justify=vj or None)})',
             f'    (property "Footprint" {q(self.footprint)} (at {num(self.x)} {num(self.y)} 0) {font(hide=True)})',
             f'    (property "Datasheet" "" (at {num(self.x)} {num(self.y)} 0) {font(hide=True)})']
        for p in s.pins:
            o.append(f'    (pin {q(p["num"])} (uuid {q(self.pin_uuids[p["num"]])}))')
        o.append(f'    (instances (project {q(PROJECT)} (path {q("/" + ROOT)} (reference {q(self.ref)}) (unit 1))))')
        o.append("  )")
        return "\n".join(o)


def place(symname, ref, value, x, y, footprint=""):
    return Part(LIB[symname], ref, value, x, y, footprint)


def wire(*pts):
    for a, b in zip(pts, pts[1:]):
        on_grid(*a); on_grid(*b)
        wires.append((a, b))


def marker(net, pt, direction):
    """Put a power symbol or a net label at pt. direction = way the wire leaves the pin."""
    if net in POWER_NETS:
        _pwr[0] += 1
        Part(LIB[{"+5V": "P5V", "+3V3": "P3V3", "GND": "GND"}[net]], f"#PWR{_pwr[0]:02d}", net, *pt)
    else:
        labels.append((net, pt, direction))


DIRS = {0: (-1, 0), 180: (1, 0), 270: (0, -1), 90: (0, 1)}


def conn(part, key, net, stub=5.08, then=None):
    """Connect a pin to a net: a short wire, then a power symbol or label. then=(dx,dy) adds a bend."""
    pt = part.end(key)
    if net == "NC":
        noconns.append(pt)
        return
    dx, dy = DIRS[part.pin(key)["ang"]]
    if stub == 0:
        marker(net, pt, (dx, dy))
        return
    a = (pt[0] + dx * stub, pt[1] + dy * stub)
    wire(pt, a)
    if then:
        b = (a[0] + then[0], a[1] + then[1])
        wire(a, b)
        a, (dx, dy) = b, (int(math.copysign(1, then[0])) if then[0] else 0, int(math.copysign(1, then[1])) if then[1] else 0)
    marker(net, a, (dx, dy))


def note(s, x, y, size=1.27):
    texts.append((s, x, y, size))


# ---------------------------------------------------------------- the circuit
note("ratatouille: cooking-coach wearable, power-bank build", 20.32, 25.4, 2.54)
note("Generated by gen_kicad.py. Edit the script, not this sheet, or your edits get overwritten.", 20.32, 30.48)

# Power in
J1 = place("USB_C_6P", "J1", "USB-C 6-pin breakout", 38.1, 50.8)
conn(J1, "VBUS", "+5V"); conn(J1, "GND", "GND")
for p in ("CC1", "CC2", "D+", "D-"):
    conn(J1, p, "NC")
note("J1 CC1/CC2: 5.1k to GND on the board\n(add 2 if missing). D+/D-: unused.", 20.32, 63.5)
JP1 = place("Jumper2", "JP1", "JP1 servo power link (fitted)", 40.64, 76.2)
conn(JP1, "1", "+5V"); conn(JP1, "2", "VSERVO")
J6 = place("NiMH_In", "J6", "NiMH 4.8 V backup (optional)", 38.1, 96.52)
conn(J6, "+", "VSERVO"); conn(J6, "-", "GND")
note("POWER\n"
     "- J1: red 6-pin USB-C board, header into the perfboard.\n"
     "  Cable: USB-A (bank's A port, plain 5 V) to USB-C.\n"
     "  Never use a 9/12 V USB-PD trigger board.\n"
     "- JP1 fitted (normal): the servos run off the bank.\n"
     "- Backup if the boards brown out: REMOVE JP1,\n"
     "  then plug a 4.8 V NiMH pack into J6.\n"
     "  Never fit JP1 with J6 plugged in.\n"
     "- All grounds are common.\n"
     "- Unplug J1 before plugging a USB cable into\n"
     "  either board to flash it.", 20.32, 111.76)

# Boards
U2 = place("ESP32_CAM", "U2", "Freenove ESP32-WROVER CAM", 111.76, 50.8)
conn(U2, "5V", "+5V"); conn(U2, "GND", "GND")
note("camera: Wi-Fi only, no signal wires", 99.06, 60.96)
U1 = place("ESP32S3_DevKit", "U1", "ESP32-S3 N16R8 DevKitC", 111.76, 99.06)
conn(U1, "5V", "+5V"); conn(U1, "GND", "GND"); conn(U1, "3V3", "+3V3")
conn(U1, "GPIO4", "PAN"); conn(U1, "GPIO5", "TILT"); conn(U1, "GPIO6", "LASER_EN")
for pin, net in (("GPIO7 BL", "TFT_BL"), ("GPIO8 RST", "TFT_RST"), ("GPIO9 DC", "TFT_DC"),
                 ("GPIO10 CS", "TFT_CS"), ("GPIO11 MOSI", "TFT_MOSI"), ("GPIO12 SCK", "TFT_SCK")):
    conn(U1, pin, net)
note("Do not use GPIO 35-37 (octal PSRAM).", 99.06, 119.38)

# Servos and their capacitors
J2 = place("Servo", "J2", "Pan servo SG90", 220.98, 45.72)
J3 = place("Servo", "J3", "Tilt servo SG90", 220.98, 66.04)
for J, sig in ((J2, "PAN"), (J3, "TILT")):
    conn(J, "SIG (orange)", sig); conn(J, "V+ (red)", "VSERVO"); conn(J, "GND (brown)", "GND")
C1 = place("C_Polarized", "C1", "1000uF 10V+", 175.26, 55.88)
C3 = place("C", "C3", "100nF (optional)", 193.04, 55.88)
c1t, c3t = C1.end("1"), C3.end("1")
top_y = c1t[1] - 2.54
wire(c1t, (c1t[0], top_y)); wire(c3t, (c3t[0], top_y))
wire((c1t[0] - 5.08, top_y), (c1t[0], top_y)); wire((c1t[0], top_y), (c3t[0], top_y))
junctions.append((c1t[0], top_y))
labels.append(("VSERVO", (c1t[0] - 5.08, top_y), (-1, 0)))
conn(C1, "2", "GND", stub=0); conn(C3, "2", "GND", stub=0)
note("C1 and C3 sit physically at the servo plugs,\nnot back at the battery. Striped leg of C1 = GND.", 160.02, 33.02)

# Laser driver
Q1 = place("Q_NPN_EBC", "Q1", "2N2222A", 205.74, 132.08)
R1 = place("R", "R1", "1k", 190.5, 128.27)
R2 = place("R", "R2", "10k", 195.58, 135.89)
J4 = place("KY008", "J4", "KY-008 laser", 233.68, 119.38)
base = Q1.end("B")
wire(R1.end("2"), R2.end("1"), base)
junctions.append(R2.end("1"))
conn(R1, "1", "LASER_EN", stub=2.54, then=(-5.08, 0))
conn(R2, "2", "GND", stub=0)
conn(Q1, "E", "GND", stub=0)
c = Q1.end("C")
k = J4.end("-")
wire(c, (c[0], k[1]), k)
conn(J4, "S", "+5V"); conn(J4, "mid (NC)", "NC")
note("LASER\n"
     "- Q1 switches the laser's low side.\n"
     "- R2 holds the base low, so the laser stays off while the S3 boots.\n"
     "- Most TO-92 2N2222A are E-B-C (flat face toward you, legs down).\n"
     "  Check yours.\n"
     "- Firmware turns the laser off 0.5 s after UDP packets stop.", 99.06, 132.08)

# Display: 1.54in 240x240 ST7789 TFT, SPI
J5 = place("TFT_ST7789", "J5", "1.54in ST7789 TFT (SPI)", 226.06, 162.56)
conn(J5, "VCC", "+3V3")
for pin, net in (("SCL", "TFT_SCK"), ("SDA", "TFT_MOSI"), ("RES", "TFT_RST"), ("DC", "TFT_DC"), ("CS", "TFT_CS"), ("BLK", "TFT_BL")):
    conn(J5, pin, net)
conn(J5, "GND", "GND")
note("TFT: SCL/SDA on the module are really SPI clock/data (SCK/MOSI).\n"
     "Power it from 3V3. Modules without a CS pin: leave TFT_CS unconnected.", 160.02, 182.88)


# ---------------------------------------------------------------- write the schematic
def label_sexpr(net, pt, d):
    ang = {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}[d]
    just = "left bottom" if ang in (0, 90) else "right bottom"
    return f'  (label {q(net)} (at {num(pt[0])} {num(pt[1])} {ang}) (fields_autoplaced) {font(justify=just)} (uuid {q(uid())}))'


def write_sch():
    o = ["(kicad_sch (version 20230121) (generator eeschema)", "",
         f"  (uuid {q(ROOT)})", "", '  (paper "A4")', "",
         "  (title_block",
         '    (title "ratatouille: cooking-coach wearable")',
         '    (date "2026-11-17")',
         '    (rev "1")',
         '    (comment 1 "Power-bank build. NiMH backup through JP1/J6.")',
         "  )", "", "  (lib_symbols"]
    o += [s.sexpr() for s in LIB.values()]
    o.append("  )")
    for x, y in junctions:
        o.append(f"  (junction (at {num(x)} {num(y)}) (diameter 0) (color 0 0 0 0) (uuid {q(uid())}))")
    for x, y in noconns:
        o.append(f"  (no_connect (at {num(x)} {num(y)}) (uuid {q(uid())}))")
    for a, b in wires:
        o.append(f"  (wire (pts (xy {num(a[0])} {num(a[1])}) (xy {num(b[0])} {num(b[1])})) (stroke (width 0) (type default)) (uuid {q(uid())}))")
    for s, x, y, size in texts:
        o.append(f"  (text {q(s)} (at {num(x)} {num(y)} 0) {font(size, justify='left top')} (uuid {q(uid())}))")
    for net, pt, d in labels:
        o.append(label_sexpr(net, pt, d))
    for p in parts:
        o.append(p.sexpr())
    o += ["", "  (sheet_instances", '    (path "/" (page "1"))', "  )", ")", ""]
    txt = "\n".join(o)
    assert txt.count("(") == txt.count(")"), "unbalanced parens"
    with open(os.path.join(HERE, PROJECT + ".kicad_sch"), "w") as fh:
        fh.write(txt)
    pro = {"meta": {"filename": PROJECT + ".kicad_pro", "version": 1},
           "sheets": [[ROOT, "Root"]], "text_variables": {}}
    with open(os.path.join(HERE, PROJECT + ".kicad_pro"), "w") as fh:
        json.dump(pro, fh, indent=2)


# ---------------------------------------------------------------- connectivity check
def check():
    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    key = lambda pt: (round(pt[0], 3), round(pt[1], 3))
    pins_at = collections.defaultdict(list)
    for p in parts:
        for pin in p.sym.pins:
            pt = key(p.end(pin["num"]))
            find(("pt", pt))
            if p.sym.power:
                union(("pt", pt), ("net", p.value))
            else:
                pins_at[pt].append(f'{p.ref}.{pin["name"] if pin["name"] != "~" else pin["num"]}')
    for a, b in wires:
        union(("pt", key(a)), ("pt", key(b)))
    # a point that lies inside a wire segment joins it (only legal at a junction)
    wire_pts = {key(a) for w in wires for a in w} | set(pins_at)
    for a, b in wires:
        for pt in wire_pts:
            if pt in (key(a), key(b)):
                continue
            if (a[0] == b[0] == pt[0] and min(a[1], b[1]) < pt[1] < max(a[1], b[1])) or \
               (a[1] == b[1] == pt[1] and min(a[0], b[0]) < pt[0] < max(a[0], b[0])):
                assert pt in {key(j) for j in junctions}, f"T-connection without junction at {pt}"
                union(("pt", pt), ("pt", key(a)))
    for net, pt, _ in labels:
        union(("pt", key(pt)), ("net", net))
    nc = {key(pt) for pt in noconns}
    nets = collections.defaultdict(list)
    for pt, names in pins_at.items():
        root = find(("pt", pt))
        nets[root] += names
    named = {}
    for n in list(parent):
        if n[0] == "net":
            named[find(n)] = n[1]
    bad = []
    print("Netlist:")
    for root, names in sorted(nets.items(), key=lambda kv: named.get(kv[0], "~")):
        name = named.get(root)
        if name is None:
            if len(names) == 1 and any(k in nc for k, v in pins_at.items() if v == names):
                continue
            name = "(unnamed)"
        print(f"  {name:10s} {', '.join(sorted(names))}")
        if len(names) < 2:
            bad.append((name, names))
    for pt, names in pins_at.items():
        if len(names) == 1 and pt not in nc and len(nets[find(("pt", pt))]) < 2:
            bad.append(("dangling", names))
    assert not bad, f"problems: {bad}"
    print("OK: every pin is on a net with at least one other pin, or marked no-connect.")


# ---------------------------------------------------------------- PNG preview
def preview():
    S = 6.0
    W, H = 297 * S, 210 * S
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}">',
           f'<rect x="0" y="0" width="{W:.0f}" height="{H:.0f}" style="fill:#f5f4ef"/>']
    WIRE, SYMC, BODY, PINC, NAMEC, LBL, NOTE = "#008400", "#840000", "#ffffc2", "#840000", "#006464", "#000000", "#00009a"

    def seg(a, b, w, c):
        (x1, y1), (x2, y2) = a, b
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1
        nx, ny = -dy / L * w / 2, dx / L * w / 2
        ex, ey = dx / L * w / 2, dy / L * w / 2
        pts = [(x1 + nx - ex, y1 + ny - ey), (x2 + nx + ex, y2 + ny + ey), (x2 - nx + ex, y2 - ny + ey), (x1 - nx - ex, y1 - ny - ey)]
        out.append('<polygon points="%s" style="fill:%s"/>' % (" ".join(f"{x * S:.1f},{y * S:.1f}" for x, y in pts), c))

    def poly(pts, c):
        out.append('<polygon points="%s" style="fill:%s"/>' % (" ".join(f"{x * S:.1f},{y * S:.1f}" for x, y in pts), c))

    def circ(cx, cy, r, c):
        out.append(f'<circle cx="{cx * S:.1f}" cy="{cy * S:.1f}" r="{r * S:.1f}" style="fill:{c}"/>')

    def text(s, x, y, size, c, anchor="start", bold=False):
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        b = ' font-weight="bold"' if bold else ""
        out.append(f'<text x="{x * S:.1f}" y="{y * S:.1f}" font-family="DejaVu Sans" font-size="{size * S * 1.15:.1f}" fill="{c}" text-anchor="{anchor}"{b}>{s}</text>')

    for a, b in wires:
        seg(a, b, 0.3, WIRE)
    for p in parts:
        T = lambda x, y, p=p: (p.x + x, p.y - y)
        sym = p.sym
        col = "#a00000" if sym.power else SYMC
        for g in sym.gfx:
            if g[0] == "rect":
                _, x1, y1, x2, y2, fill = g
                a, b = T(x1, y1), T(x2, y2)
                if fill == "background":
                    poly([a, (b[0], a[1]), b, (a[0], b[1])], BODY)
                if fill == "outline":
                    poly([a, (b[0], a[1]), b, (a[0], b[1])], col)
                for u, v in ((a, (b[0], a[1])), ((b[0], a[1]), b), (b, (a[0], b[1])), ((a[0], b[1]), a)):
                    seg(u, v, 0.254, col)
            elif g[0] == "poly":
                _, pts, w, fill = g
                pts = [T(*xy) for xy in pts]
                if fill == "outline":
                    poly(pts, col)
                for u, v in zip(pts, pts[1:]):
                    seg(u, v, max(w, 0.2), col)
            elif g[0] == "circle":
                _, cx, cy, r, fill = g
                cx, cy = T(cx, cy)
                circ(cx, cy, r + 0.127, col); circ(cx, cy, r - 0.127, BODY if fill != "none" else "#f5f4ef")
            elif g[0] == "text":
                _, s, x, y, size = g
                x, y = T(x, y)
                text(s, x, y + size * 0.4, size, col, "middle")
        for pin in sym.pins:
            if pin["len"] == 0:
                continue
            ex, ey = p.end(pin["num"])
            dx, dy = DIRS[pin["ang"]]
            inner = (ex - dx * pin["len"], ey - dy * pin["len"])
            seg((ex, ey), inner, 0.2, PINC)
            if not (sym.hide_names or sym.power) and pin["name"] != "~":
                if pin["ang"] == 0:
                    text(pin["name"], inner[0] + 1.0, ey + 0.5, 1.27, NAMEC)
                elif pin["ang"] == 180:
                    text(pin["name"], inner[0] - 1.0, ey + 0.5, 1.27, NAMEC, "end")
        if not sym.power:
            rx, ry, rj = sym.ref_at
            vx, vy, vj = sym.val_at
            anchor = "start" if rj == "left" else "middle"
            ry_off = -0.3 if rj == "bottom" else (1.0 if rj == "top" else 0.5)
            text(p.ref, p.x + rx, p.y - ry + ry_off, 1.27, SYMC, anchor, True)
            vy_off = 1.2 if vj == "top" else 0.5
            text(p.value, p.x + vx, p.y - vy + vy_off, 1.27, SYMC, "start" if vj == "left" else "middle")
        else:
            vx, vy, _ = sym.val_at
            text(p.value, p.x + vx, p.y - vy + (0 if vy > 0 else 1.2), 1.27, col, "middle")
    for x, y in junctions:
        circ(x, y, 0.5, WIRE)
    for x, y in noconns:
        seg((x - 0.635, y - 0.635), (x + 0.635, y + 0.635), 0.2, "#0000c8")
        seg((x - 0.635, y + 0.635), (x + 0.635, y - 0.635), 0.2, "#0000c8")
    for net, (x, y), d in labels:
        if d == (1, 0):
            text(net, x + 0.3, y - 0.4, 1.27, LBL)
        elif d == (-1, 0):
            text(net, x - 0.3, y - 0.4, 1.27, LBL, "end")
        else:
            text(net, x + 0.6, y + 0.5, 1.27, LBL)
    for s, x, y, size in texts:
        for i, line in enumerate(s.split("\n")):
            text(line, x, y + size * 1.0 + i * size * 1.45, size, NOTE, bold=size > 2)
    out.append("</svg>")
    svg = os.path.join(HERE, "preview.svg")
    with open(svg, "w") as fh:
        fh.write("\n".join(out))
    subprocess.run(["magick", "MSVG:" + svg, os.path.join(HERE, "preview.png")], check=True)
    os.remove(svg)


if __name__ == "__main__":
    write_sch()
    check()
    preview()
    print("wrote", PROJECT + ".kicad_sch", PROJECT + ".kicad_pro", "preview.png")
