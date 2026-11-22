#!/usr/bin/env python3
"""Assembled-view mockup of the chest box: enclosure + boards + pan-tilt + display + wiring.

Reads the sizes from enclosure.scad so it stays in sync, then writes:
  assembly.png   3 views, drawn with a tiny built-in renderer (no OpenSCAD needed)
  assembly.scad  the same parts as one standalone OpenSCAD file you can spin around

The parts are blocks with roughly the right size, not exact models. Sizes the team hasn't
measured yet (pan-tilt height, TFT board size) are guesses; measure and update the variables.

Run: python3 assembly.py   (needs ImageMagick `convert` for the PNG)
"""
import math, re, subprocess, pathlib

HERE = pathlib.Path(__file__).resolve().parent

# ---------------------------------------------------------------- read enclosure.scad
SRC = (HERE / "enclosure.scad").read_text()
P = {}
for m in re.finditer(r"^(\w+)\s*=\s*([^;]+);", SRC, re.M):
    name, expr = m.group(1), m.group(2)
    try:
        P[name] = eval(expr.replace("true", "True").replace("false", "False"), {}, P)
    except Exception:
        pass
g = P.get
W, IW, IH, ID, LT = g("wall"), g("in_w"), g("in_h"), g("in_d"), g("lid_t")
OW, OH, OD = IW + 2 * W, IH + 2 * W, ID + W

# ---------------------------------------------------------------- parts
# box: ("box", (x0,x1,y0,y1,z0,z1), colour, group)
# cyl: ("cyl", axis, (c1,c2), r, (a0,a1), colour, group)   axis "x"/"y"/"z"; c1,c2 = the other two coords
# wire: ("wire", [points], colour, width)
# group: "in" (inside the box), "out" (outside), "lid"
parts = []
def box(x0, x1, y0, y1, z0, z1, col, grp="in"):
    parts.append(("box", (min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1), min(z0, z1), max(z0, z1)), col, grp))
def cyl(axis, c1, c2, r, a0, a1, col, grp="in"):
    parts.append(("cyl", axis, (c1, c2), r, (a0, a1), col, grp))
def wire(pts, col, w=2.0):
    parts.append(("wire", pts, col, w))

PCB_G, PCB_B, BLACK, SILVER = "#2f7d4a", "#2b4f9e", "#222222", "#b8bcc2"
PERF, HDR, ORANGE, BLUE = "#c9a46a", "#1a1a1a", "#e08a1e", "#3d6fd1"

# carrier perfboard (boards plug into its front, wiring on its back)
y_cf = W + g("post_len"); y_cb = y_cf + 1.6
cx_mid = W + IW / 2
box(cx_mid - 41, cx_mid + 41, y_cf, y_cb, W + 3.5, W + 73.5, PERF)   # 82 x 70
for px, pz in g("post_xz"):
    cyl("y", W + px, W + pz, g("post_r"), W, y_cf, "#d9d2c3")
# corner pillars (lid screw bosses)
bi = g("bi")
for cx in (W + bi, W + IW - bi):
    for cz in (W + bi, W + IH - bi):
        cyl("y", cx, cz, g("boss_r"), W, OD - LT, "#d9d2c3")

# female headers, board PCBs, modules
y_hf = y_cf - 8.5; y_pf = y_hf - 1.6; y_mf = y_pf - 3.2
z0 = W + g("cam_board_z0")
cam_c, s3_c = W + g("cam_x"), W + g("s3_x")
for c, ln in ((cam_c, 20), (s3_c, 22)):
    for dx in (-11.43, 11.43):
        box(c + dx - 1.3, c + dx + 1.3, y_hf, y_cf, z0 + 2, z0 + 2 + ln * 2.54, HDR)
# camera board (Freenove WROVER CAM): 28 x 57, antenna end up
box(cam_c - 14, cam_c + 14, y_pf, y_hf, z0, z0 + 57, PCB_G)
box(cam_c - 9, cam_c + 9, y_mf, y_pf, z0 + 32, z0 + 57, SILVER)       # WROVER shield
box(cam_c - 9, cam_c + 9, y_mf, y_pf, z0 + 57, z0 + 63.5, "#2d2d5a")  # antenna overhang
box(cam_c - 4.5, cam_c + 4.5, y_mf, y_pf, z0 - 0.5, z0 + 7, SILVER)   # USB-C
lens_x, lens_z = cam_c + g("cam_lens_dx"), W + g("lens_z")
box(lens_x - 4.25, lens_x + 4.25, y_mf - 5, y_mf, lens_z - 4.25, lens_z + 4.25, BLACK)  # OV3660
cyl("y", lens_x, lens_z, 3, y_mf - 7.5, y_mf - 5, "#444444")                       # lens barrel
box(lens_x - 4, lens_x + 4, y_mf - 0.3, y_mf, lens_z - 14, lens_z - 4.25, ORANGE)    # ribbon
# ESP32-S3 board: 28 x 64, two USB-C at the bottom
box(s3_c - 14, s3_c + 14, y_pf, y_hf, z0, z0 + 64, "#1e1e1e")
box(s3_c - 9, s3_c + 9, y_mf, y_pf, z0 + 38.5, z0 + 56.5, SILVER)      # WROOM shield
box(s3_c - 9, s3_c + 9, y_mf, y_pf, z0 + 56.5, z0 + 64, "#2d2d5a")    # antenna
for dx in (-7, 7):
    box(s3_c + dx - 4.5, s3_c + dx + 4.5, y_mf, y_pf, z0 - 0.5, z0 + 7, SILVER)

# parts on the back of the carrier
box(s3_c - 7, s3_c + 7, y_cb, y_cb + 1.6, W + 14, W + 32, PCB_B)     # J1 USB-C breakout, between the S3 header rows
box(s3_c - 4.5, s3_c + 4.5, y_cb + 1.6, y_cb + 4.8, W + 22, W + 32, SILVER)  # USB-C socket, opening down
box(s3_c - 3.5, s3_c + 3.5, y_cb + 2, y_cb + 4.4, W + 10, W + 22, "#555555")  # USB-C plug of the bank cable
cyl("x", y_cb + 4.5, W + 42, 4, 50, 62, "#2e3f8f")                   # C1 1000uF lying down
box(40, 44, y_cb, y_cb + 4, W + 34, W + 38, BLACK)                   # Q1
cyl("z", 34, y_cb + 1.5, 1, W + 30, W + 36, "#d8c39a")               # R1
cyl("z", 37, y_cb + 1.5, 1, W + 30, W + 36, "#d8c39a")               # R2
for x0 in (18, 28):                                                  # J2/J3 servo headers
    box(x0, x0 + 7.6, y_cb, y_cb + 2.5, W + 70, W + 72.5, HDR)
box(cx_mid - 3, cx_mid + 3, y_cb, y_cb + 2.5, W + 54, W + 59, HDR)   # JP1

# ---- outside: pan-tilt on the top
OUT = "out"
ptx, pty = W + g("pt_x"), W + g("pt_y")
top = OH
box(ptx - 19, ptx + 19, pty - 16, pty + 16, top, top + 2, BLACK, OUT)           # base plate 38 x 32 (measured)
box(ptx - 6.1, ptx + 6.1, pty - 11.5, pty + 11.5, top + 2, top + 25, BLUE, OUT)  # pan servo
box(ptx - 16, ptx + 16, pty - 8, pty + 8, top + 25, top + 28, BLACK, OUT)        # bracket
box(ptx - 2, ptx + 2, pty - 8, pty + 8, top + 28, top + 52, BLACK, OUT)          # bracket arm
box(ptx + 2, ptx + 14.2, pty - 11.5, pty + 11.5, top + 31, top + 57, BLUE, OUT)  # tilt servo
pz = top + 59                                                                    # platform 35 x 38, ~62 total (measured)
box(ptx - 17.5, ptx + 17.5, pty - 19, pty + 19, pz, pz + 2, BLACK, OUT)
for sx in (-1, 1):                                                               # its 2 clip tabs, 30.25 apart
    box(ptx + sx * 15.125, ptx + sx * 16.125, pty - 5, pty + 5, pz + 2, pz + 13, BLACK, OUT)
# laser_mount.scad v3: tray that snaps between the tabs, hood over the head
HOLD = "#e6dcc4"
hz0 = pz + 2
box(ptx - 15.575, ptx + 15.575, pty - 11, pty + 11, hz0, hz0 + 1.6, HOLD, OUT)            # floor
for sx in (-1, 1):
    box(ptx + sx * 15.575 - (2.4 if sx > 0 else 0), ptx + sx * 15.575 + (0 if sx > 0 else 2.4),
        pty - 11, pty + 11, hz0, hz0 + 9.4, HOLD, OUT)                                    # snap walls
box(ptx - 7.9, ptx + 10.3, pty - 7, pty - 1, hz0, hz0 + 11.5, HOLD, OUT)                 # hood
box(ptx - 5.8, ptx + 8.2, pty - 7, pty + 11, hz0 + 1.6, hz0 + 2.6, "#c0392b", OUT)      # KY-008 PCB
cyl("y", ptx, hz0 + 6.65, 3.05, pty - 11, pty - 7, SILVER, OUT)                          # laser head nose
# TFT glued on the top (board size unknown: drawn 31 x 35)
dx_, dy_ = W + g("disp_x"), W + g("disp_y")
box(dx_ - 15.5, dx_ + 15.5, dy_ - 2.5, dy_ + 32.5, top, top + 1.6, PCB_B, OUT)
box(dx_ - 14, dx_ + 14, dy_ + 1, dy_ + 29, top + 1.6, top + 3, "#101820", OUT)
# power bank cable coming up through the floor notch
cab_x = W + g("cable_x")

# ---- wires
yh = W + g("wire_hole_y") + g("wire_hole")[0] / 2      # middle of the side wire hole
zh = W + g("wire_hole_z") + g("wire_hole")[1] / 2
wire([(ptx + 8, pty, top + 40), (ptx - 21, pty, top + 20), (-2, yh, top - 2), (-2, yh, zh),
      (5, yh, zh), (6, y_cb + 4, zh + 2), (21, y_cb + 3, W + 71)], ORANGE, 3)             # servo cables
wire([(ptx + 2, pty + 10, top + 60), (ptx - 19, pty + 6, top + 22), (-1, yh + 3, top - 1), (-1, yh + 3, zh - 2),
      (5, yh + 3, zh - 2), (7, y_cb + 4, zh), (41, y_cb + 3, W + 36)], "#c0392b", 2)      # laser wires
wire([(dx_, dy_, top - W), (dx_, dy_, top - 8), (dx_, y_cb + 3, top - 5), (dx_, y_cb + 3, W + 74)],
     "#7d3cc8", 3)                                                                       # TFT 8 wires
wire([(cab_x, OD - 5, -35), (cab_x, OD - 5, W + 2), (cab_x, y_cb + 4, W + 8)], "#333333", 4)  # power cable

# ---------------------------------------------------------------- the lid (for the back view)
def lid_parts(dy, dx=0):
    grp = "lid"
    y0 = OD - LT + dy
    box(dx + W - 1.2, dx + OW - W + 1.2, y0, y0 + LT, W - 1.2, OH - W + 1.2, "#e8e2d4", grp)
    for x in (dx + OW / 2 - g("strap_dx") - 5, dx + OW / 2 + g("strap_dx") - 5):   # strap loops
        box(x, x + 10, y0 + LT, y0 + LT + g("strap_t") + 2.4, OH / 2 - (g("strap_w") + 6) / 2, OH / 2 + (g("strap_w") + 6) / 2, "#d9d2c3", grp)
    for ex in g("lanyard_x"):                # eyelets
        box(OW / 2 + dx + ex - 2, OW / 2 + dx + ex + 2, y0 + LT, y0 + LT + 10, OH - W - 14, OH - W - 4, "#d9d2c3", grp)

# ---------------------------------------------------------------- tiny renderer
def faces_of(p):
    """Convex polygon faces with outward normals for a box/cyl."""
    out = []
    if p[0] == "box":
        x0, x1, y0, y1, z0, z1 = p[1]
        V = lambda i, j, k: ((x0, x1)[i], (y0, y1)[j], (z0, z1)[k])
        out += [([V(0,0,0), V(0,1,0), V(0,1,1), V(0,0,1)], (-1,0,0)),
                ([V(1,0,0), V(1,0,1), V(1,1,1), V(1,1,0)], (1,0,0)),
                ([V(0,0,0), V(0,0,1), V(1,0,1), V(1,0,0)], (0,-1,0)),
                ([V(0,1,0), V(1,1,0), V(1,1,1), V(0,1,1)], (0,1,0)),
                ([V(0,0,0), V(1,0,0), V(1,1,0), V(0,1,0)], (0,0,-1)),
                ([V(0,0,1), V(0,1,1), V(1,1,1), V(1,0,1)], (0,0,1))]
    else:
        _, ax, (c1, c2), r, (a0, a1), *_ = p
        n = 16
        def pt(a, t):
            u, v = c1 + r * math.cos(t), c2 + r * math.sin(t)
            return {"x": (a, u, v), "y": (u, a, v), "z": (u, v, a)}[ax]
        def nv(t):
            u, v = math.cos(t), math.sin(t)
            return {"x": (0, u, v), "y": (u, 0, v), "z": (u, v, 0)}[ax]
        ts = [2 * math.pi * i / n for i in range(n)]
        for i in range(n):
            t0, t1 = ts[i], ts[(i + 1) % n]
            out.append(([pt(a0, t0), pt(a1, t0), pt(a1, t1), pt(a0, t1)], nv((t0 + t1) / 2)))
        axn = {"x": (1,0,0), "y": (0,1,0), "z": (0,0,1)}[ax]
        out.append(([pt(a1, t) for t in ts], axn))
        out.append(([pt(a0, t) for t in ts], tuple(-c for c in axn)))
    return out

def shade(col, k):
    c = col.lstrip("#"); rgb = [int(c[i:i+2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v * k))) for v in rgb)

class View:
    def __init__(self, az, el):
        self.a, self.e = math.radians(az), math.radians(el)
    def rot(self, p):
        x, y, z = p
        ca, sa, ce, se = math.cos(self.a), math.sin(self.a), math.cos(self.e), math.sin(self.e)
        x1 = x * ca - y * sa; y1 = x * sa + y * ca
        return x1, y1 * se + z * ce, y1 * ce - z * se      # screen x, screen up, depth
    def dirv(self, n):
        return self.rot(n)[2]                              # <0 means facing the viewer

LIGHT = (0.35, -0.55, 0.75)
def light_k(n):
    l = math.sqrt(sum(c * c for c in LIGHT))
    return 0.62 + 0.45 * max(0.0, sum(a * b for a, b in zip(n, LIGHT)) / l)

def shell_faces(cut_right=False, lid_on=True):
    """Ghost faces of the box + dark marks for its holes."""
    F = []
    fw = [((0,0,0), (OW,0,0), (OW,0,OH), (0,0,OH)), (0,-1,0)]
    F.append(fw)
    F.append((((0,0,0), (0,0,OH), (0,OD,OH), (0,OD,0)), (-1,0,0)))
    if not cut_right:
        F.append((((OW,0,0), (OW,OD,0), (OW,OD,OH), (OW,0,OH)), (1,0,0)))
    F.append((((0,0,0), (0,OD,0), (OW,OD,0), (OW,0,0)), (0,0,-1)))
    F.append((((0,0,OH), (OW,0,OH), (OW,OD,OH), (0,OD,OH)), (0,0,1)))
    if lid_on:
        F.append((((0,OD,0), (OW,OD,0), (OW,OD,OH), (0,OD,OH)), (0,1,0)))
    holes = []
    cw, ch = g("cam_win_w") + 2 * g("cam_win_flare"), g("cam_win_h") + 2 * g("cam_win_flare")
    wz = W + g("lens_z") - g("cam_win_drop")
    holes.append(([(lens_x - cw/2, -0.05, wz - ch/2), (lens_x + cw/2, -0.05, wz - ch/2),
                   (lens_x + cw/2, -0.05, wz + ch/2), (lens_x - cw/2, -0.05, wz + ch/2)], (0,-1,0)))
    y0, y1 = W + g("wire_hole_y"), W + g("wire_hole_y") + g("wire_hole")[0]
    hz0, hz1 = W + g("wire_hole_z"), W + g("wire_hole_z") + g("wire_hole")[1]
    holes.append(([(-0.05, y0, hz0), (-0.05, y0, hz1), (-0.05, y1, hz1), (-0.05, y1, hz0)], (-1,0,0)))
    sw, sl = g("disp_slot")
    holes.append(([(dx_ - sw/2, dy_ - sl/2, OH + 0.05), (dx_ + sw/2, dy_ - sl/2, OH + 0.05),
                   (dx_ + sw/2, dy_ + sl/2, OH + 0.05), (dx_ - sw/2, dy_ + sl/2, OH + 0.05)], (0,0,1)))
    return F, holes

def project_poly(v, pts, s, ox, oy):
    return " ".join("%.1f,%.1f" % (ox + s * v.rot(p)[0], oy - s * v.rot(p)[1]) for p in pts)

PANEL_W = 760
def render_panel(v, plist, s, ox, oy, title, labels, cut_right=False, lid_on=True, ox_panel=0):
    svg = []
    shell, holes = shell_faces(cut_right, lid_on)
    # 1. ghost faces facing away (the far inside walls)
    for pts, n in shell:
        if v.dirv(n) >= 0:
            svg.append('<polygon points="%s" fill="#f3efe6" fill-opacity="0.55" stroke="#9a9384" stroke-width="1"/>' % project_poly(v, pts, s, ox, oy))
    def draw_group(items):
        objs = []
        for p in items:
            if p[0] == "wire":
                pts = p[1]
                for a, b in zip(pts, pts[1:]):
                    d = (v.rot(a)[2] + v.rot(b)[2]) / 2
                    ax_, ay_ = ox + s * v.rot(a)[0], oy - s * v.rot(a)[1]
                    bx_, by_ = ox + s * v.rot(b)[0], oy - s * v.rot(b)[1]
                    L_ = math.hypot(bx_ - ax_, by_ - ay_) or 1
                    hw = p[3] * s / 3.2                     # half width; drawn as a polygon (ImageMagick draws <line> badly)
                    nx, ny = -(by_ - ay_) / L_ * hw, (bx_ - ax_) / L_ * hw
                    objs.append((d, '<polygon points="%.1f,%.1f %.1f,%.1f %.1f,%.1f %.1f,%.1f" fill="%s" stroke="%s" stroke-width="0.8"/>'
                                 % (ax_ + nx, ay_ + ny, bx_ + nx, by_ + ny, bx_ - nx, by_ - ny, ax_ - nx, ay_ - ny, p[2], shade(p[2], 0.6))))
                continue
            fs = faces_of(p)
            cen = [sum(c) / len(c) for c in zip(*[q for f, _ in fs for q in f])]
            d = v.rot(cen)[2]
            col = p[2] if p[0] == "box" else p[5]
            body = []
            for pts, n in fs:
                if v.dirv(n) < 0:
                    body.append('<polygon points="%s" fill="%s" stroke="%s" stroke-width="0.6"/>' % (project_poly(v, pts, s, ox, oy), shade(col, light_k(n)), shade(col, 0.5)))
            objs.append((d, "".join(body)))
        for _, el in sorted(objs, key=lambda t: -t[0]):
            svg.append(el)
    inside = [p for p in plist if p[0] != "wire" and p[-1] == "in"]
    outside = [p for p in plist if p[0] != "wire" and p[-1] in ("out", "lid")]
    def inside_pt(q): return 0 < q[0] < OW and 0 < q[1] < OD and 0 < q[2] < OH
    wires_in, wires_out = [], []
    for p in plist:
        if p[0] == "wire":
            pts = p[1]
            for a, b in zip(pts, pts[1:]):
                mid = tuple((i + j) / 2 for i, j in zip(a, b))
                (wires_in if inside_pt(mid) else wires_out).append(("wire", [a, b], p[2], p[3]))
    draw_group(inside + wires_in)
    # 2. ghost faces facing the viewer, then hole marks, then everything outside the box
    for pts, n in shell:
        if v.dirv(n) < 0:
            svg.append('<polygon points="%s" fill="#f3efe6" fill-opacity="0.28" stroke="#6f6a5e" stroke-width="1.2"/>' % project_poly(v, pts, s, ox, oy))
    for pts, n in holes:
        if v.dirv(n) < 0:
            svg.append('<polygon points="%s" fill="#000" fill-opacity="0.35" stroke="#000" stroke-width="0.8"/>' % project_poly(v, pts, s, ox, oy))
    draw_group(outside + wires_out)
    svg.append('<text x="%.0f" y="40" font-family="DejaVu Sans" font-size="24" font-weight="bold" text-anchor="middle">%s</text>' % (ox_panel + PANEL_W / 2, title))
    for text, p3, (dxl, dyl) in labels:
        X, Y = ox + s * v.rot(p3)[0], oy - s * v.rot(p3)[1]
        tx, ty = X + dxl, Y + dyl
        anchor = "start" if dxl >= 0 else "end"
        tw = 9 * len(text) + 12
        left, right = ox_panel, ox_panel + PANEL_W
        if anchor == "end" and tx - tw < left: tx = left + tw
        if anchor == "start" and tx + tw > right: tx = right - tw
        svg.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#c0392b" stroke-width="1.5"/><circle cx="%.1f" cy="%.1f" r="3" fill="#c0392b"/>' % (X, Y, tx, ty, X, Y))
        svg.append('<text x="%.1f" y="%.1f" font-family="DejaVu Sans" font-size="16" text-anchor="%s" stroke="#fff" stroke-width="4" paint-order="stroke">%s</text>' % (tx + (4 if dxl >= 0 else -4), ty + 5, anchor, text))
    return svg

def main():
    PW, PH, S = 760, 820, 3.3
    panels = []
    L = lambda t, p, d: (t, p, d)
    # view 1: front-right, everything in place, box see-through
    v1 = View(-32, 24)
    lab1 = [L("laser (KY-008) in snap-in holder", (ptx, pty - 11, hz0 + 6.65), (-120, -30)),
            L("pan-tilt (2x SG90)", (ptx + 6, pty - 11.5, top + 15), (-150, 10)),
            L("TFT on top", (dx_ + 10, dy_ + 5, top + 2), (60, -60)),
            L("camera lens + window", (lens_x, -0.5, lens_z), (-150, 20)),
            L("CAM board", (cam_c - 10, y_mf, z0 + 20), (-150, 40)),
            L("S3 board", (s3_c + 10, y_mf, z0 + 30), (110, 30)),
            L("perfboard carrier", (cx_mid + 30, y_cf, W + 78), (90, -50)),
            L("servo + laser wires -> side hole", (-1, yh, zh), (-60, 90)),
            L("power bank cable (in pocket)", (cab_x, OD - 5, -30), (70, 10)),
            L("USB-C ports -> floor slots", (s3_c - 7, y_mf, z0), (120, 70))]
    # view 2: back-left, lid pulled off to show the back of the carrier
    v2 = View(240, 26)
    plist2 = parts + []
    saved = list(parts)
    lid_parts(25, 0)
    plist2 = list(parts); parts[:] = saved
    lab2 = [L("lid (strap loops + 2 lanyard eyelets)", (OW - 10, OD + 25 + LT, OH / 2), (60, 110)),
            L("back of carrier: wiring, Q1, R1/R2, C1", (52, y_cb + 4, W + 42), (100, 60)),
            L("USB-C breakout (J1) + bank cable", (s3_c, y_cb + 6, W + 20), (-60, 120)),
            L("corner pillars (lid screws)", (W + bi, OD - LT, W + IH - bi), (-140, -40)),
            L("servo headers J2/J3", (24, y_cb + 2, W + 71), (-120, 40))]
    # view 3: from the right side, right wall cut away: shows the front-to-back stack
    v3 = View(-62, 12)
    lab3 = [L("lens behind the window", (lens_x, y_mf - 6, lens_z), (-120, 150)),
            L("boards plug into headers", (s3_c + 14, y_hf, z0 + 50), (120, -130)),
            L("carrier", (cx_mid + 35, y_cb, W + 60), (120, -20)),
            L("back = lid (chest side)", (OW, OD, OH / 2), (90, 30)),
            L("pan-tilt + laser", (ptx + 6, pty, top + 15), (130, -20))]
    views = [(v1, parts, "1. Front (box see-through)", lab1, {}),
             (v2, plist2, "2. Back, lid pulled off", lab2, {"lid_on": False}),
             (v3, parts, "3. Right side, wall removed", lab3, {"cut_right": True})]
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d"><rect width="100%%" height="100%%" fill="#ffffff"/>' % (PW * 3, PH)]
    for i, (v, plist, title, lab, kw) in enumerate(views):
        # centre the panel on the projected bounding box
        pts = []
        for p in plist:
            if p[0] == "wire":
                pts += p[1]
            else:
                pts += [q for f, _ in faces_of(p) for q in f]
        pts += [(0,0,0), (OW,OD,OH), (0,OD,0), (OW,0,OH)]
        xs = [v.rot(q)[0] for q in pts]; ys = [v.rot(q)[1] for q in pts]
        s = min(S, (PW - 330) / (max(xs) - min(xs)), (PH - 160) / (max(ys) - min(ys)))
        ox = PW * i + PW / 2 - s * (max(xs) + min(xs)) / 2
        oy = PH / 2 + 20 + s * (max(ys) + min(ys)) / 2
        out += render_panel(v, plist, s, ox, oy, title, lab, ox_panel=PW * i, **kw)
    out.append('<text x="12" y="%d" font-family="DejaVu Sans" font-size="15" fill="#555">Rough blocks, not exact models. Pan-tilt + laser measured (rough). Still guesses: TFT board (31 x 35 mm), board positions. Generated by hardware/enclosure/assembly.py from enclosure.scad.</text>' % (PH - 12))
    out.append("</svg>")
    (HERE / "assembly.svg").write_text("\n".join(out))
    subprocess.run(["convert", "-density", "96", str(HERE / "assembly.svg"), str(HERE / "assembly.png")], check=True)
    (HERE / "assembly.svg").unlink()
    write_scad(plist2)
    print("wrote assembly.png and assembly.scad")

# ---------------------------------------------------------------- standalone OpenSCAD assembly
def write_scad(plist_with_lid):
    body = SRC.split("// Print layout:")[0]
    body = re.sub(r'^part = .*\n', '', body, flags=re.M)
    lines = ["// ASSEMBLY VIEW (look only, don't print). Standalone, one file. F5 to preview, drag to spin.",
             "// Generated by assembly.py from enclosure.scad. Parts are rough blocks; unmeasured sizes are guesses.",
             "ghost = true;   // see-through box (works in F5 preview)",
             "show_lid = false;", "", body,
             "if (ghost) color([0.95, 0.93, 0.88], 0.3) shell(); else color(\"beige\") shell();",
             "if (show_lid) color(\"beige\") translate([wall - rebate + fit, OD + 45, wall - rebate + fit]) rotate([90, 0, 0]) mirror([0, 0, 1]) lid();",
             ""]
    for p in parts:
        if p[0] == "box":
            x0, x1, y0, y1, z0, z1 = p[1]
            lines.append('color("%s") translate([%.2f, %.2f, %.2f]) cube([%.2f, %.2f, %.2f]);' % (p[2], x0, y0, z0, x1 - x0, y1 - y0, z1 - z0))
        elif p[0] == "cyl":
            _, ax, (c1, c2), r, (a0, a1), col, _g = p
            if ax == "y":
                lines.append('color("%s") translate([%.2f, %.2f, %.2f]) rotate([-90, 0, 0]) cylinder(r = %.2f, h = %.2f, $fn = 24);' % (col, c1, a0, c2, r, a1 - a0))
            elif ax == "x":
                lines.append('color("%s") translate([%.2f, %.2f, %.2f]) rotate([0, 90, 0]) cylinder(r = %.2f, h = %.2f, $fn = 24);' % (col, a0, c1, c2, r, a1 - a0))
            else:
                lines.append('color("%s") translate([%.2f, %.2f, %.2f]) cylinder(r = %.2f, h = %.2f, $fn = 24);' % (col, c1, c2, a0, r, a1 - a0))
        else:
            pts, col, w = p[1], p[2], p[3]
            for a, b in zip(pts, pts[1:]):
                lines.append('color("%s") hull() { translate([%.1f, %.1f, %.1f]) sphere(d = %.1f, $fn = 8); translate([%.1f, %.1f, %.1f]) sphere(d = %.1f, $fn = 8); }' % (col, *a, w, *b, w))
    (HERE / "assembly.scad").write_text("\n".join(lines) + "\n")

if __name__ == "__main__":
    main()
