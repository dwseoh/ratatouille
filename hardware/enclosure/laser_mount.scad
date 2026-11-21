// Laser holder v3: snaps between the 2 clip tabs on the pan-tilt's top platform. No zip ties.
// STANDALONE PRINT FILE (one file, for web OpenSCAD).
//
// How it holds:
//  - Platform: its 2 upright tabs (1 mm thick, ~11 mm tall, ~1 mm hook at the top pointing inward)
//    are ~30.25 mm apart. The holder is a tray exactly that wide: push it down between the tabs,
//    they flex out, and the hooks click over the top of its side walls.
//  - Laser: the KY-008 slides in from the BACK, flat on the tray floor, until its PCB hits the
//    small front stop. The brass head goes through the hood, which keeps it from lifting or turning.
//    The pins stick out the back. A dab of hot glue at the back of the PCB stops it sliding out.
//
// Measured (rough, 2026-11-19): tab gap 30.25, tab height 11, platform 35 x 38,
// laser head ~5 dia, PCB 9 x 18 x 1, PCB bottom to head top ~7-8.
// v3 (after test print 1): tray was loose between the tabs, PCB is 14 wide (not 9),
// head hole was ~3 mm too small.
// v4 (test print 2): hole 6.5 is right, head 1.0 higher, PCB slot 15 wide x 2 tall, head off-centre (head_dx).
// It's a 10-minute print: print one, test it, adjust the numbers marked TUNE.
//
// Print: flat as drawn (floor on the bed), no supports. Beam points to -y.

/* [Platform clip] */
tab_gap  = 30.25;  // inside gap between the 2 tabs
side_fit = -0.9;   // TUNE: tray width = tab_gap - side_fit = 31.15. v2 0.35 and v3 0.1 were loose; test print
                   // said +1 mm. Negative = wider than the measured gap (the tabs flex out to grip it).
snap_h   = 9.4;    // TUNE: tray wall height = where the hooks catch (tab height 11 - hook ~1.5).
                   // Hooks don't click over: lower it 0.5. Tray rattles up/down: raise it 0.3.
side_t   = 2.4;    // tray wall thickness (the hooks rest on it)
entry    = 0.8;    // chamfer on the bottom outer edges so the tray pushes the tabs apart

/* [Laser KY-008] */
head_d   = 6.1;    // TUNE: brass head diameter. Hole = head_d + head_fit = 6.5 (v2's 5.4 hole was too small).
head_fit = 0.4;    // added to the hood bore
head_lift = 1.0;   // TUNE: head centre sits this much higher than "head resting on the PCB" (test print 2)
head_dx  = 1.2;    // TUNE: the head is off-centre on the board. Head centre is this much LEFT of the
                   // board's centre, looking at the front (beam coming at you). Measured ~6.5 of 15. Wrong side: -1.2
head_out = 4;      // how far the head sticks out past the PCB's front edge
pcb      = [14, 18];  // [across the beam (side to side), along the beam (head -> pins)]
pcb_t    = 1.0;
pcb_fit  = 1.0;    // TUNE: extra slot width (v3 had 0.4)
pcb_t_fit = 1.0;   // TUNE: extra slot height (v3 had 0.4, too tight)
hood_len = 6;      // how much of the head the hood covers, from the PCB's front edge back
hood_t   = 1.6;

/* [Tray] */
floor_t  = 1.6;

$fn = 48;

W  = tab_gap - side_fit;       // tray width
pw = pcb[0] + pcb_fit;         // PCB slot width
L  = head_out + pcb[1];        // tray length: head nose at the front edge, PCB back edge at the back
y_pcb = head_out;              // PCB front edge
hz = floor_t + pcb_t + head_d / 2 + head_lift;   // head centre height
bore = head_d + head_fit;
hw = pw + 2 * hood_t;          // hood width

module tray_side(s) {   // one side wall with the entry chamfer on its bottom outer edge
    translate([s > 0 ? W / 2 - side_t : -W / 2, 0, 0])
        difference() {
            cube([side_t, L, snap_h]);
            translate([s > 0 ? side_t : 0, -1, 0]) rotate([0, 45, 0])
                translate([-entry * 0.7071, 0, -entry * 0.7071]) cube([entry * 1.4142, L + 2, entry * 1.4142]);
        }
}

module laser_mount() {
    difference() {
        union() {
            translate([-W / 2, 0, 0]) cube([W, L, floor_t]);   // floor
            tray_side(-1); tray_side(1);                       // walls the tab hooks click over
            // hood over the head, just behind the PCB's front edge
            translate([0, y_pcb, 0]) hull() {
                translate([head_dx - hw / 2, 0, 0]) cube([hw, hood_len, floor_t + pcb_t + pcb_t_fit + 1]);
                translate([0, 0, hz]) rotate([-90, 0, 0]) cylinder(d = bore + 2 * hood_t, h = hood_len);
            }
            // front stop: a low bar the PCB edge butts against (the head passes over it)
            translate([head_dx - pw / 2, y_pcb - 1.5, 0]) cube([pw, 1.5, floor_t + pcb_t + pcb_t_fit]);
        }
        // PCB slot through the hood, open at the back. It reaches up to the bottom of the head hole
        // so the two join (hole = plain circle sitting on the slot).
        translate([head_dx - pw / 2, y_pcb, floor_t]) cube([pw, L, max(pcb_t + pcb_t_fit, hz - bore / 2 - floor_t + 0.1)]);
        // head hole through the hood
        translate([0, y_pcb - 2, hz]) rotate([-90, 0, 0]) cylinder(d = bore, h = hood_len + 4);
    }
}

laser_mount();
