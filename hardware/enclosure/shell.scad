// STANDALONE PRINT FILE: THE BOX. Single file, nothing else needed.
// F6 (render), then export as STL. Already oriented for printing, no supports.
// Copied from enclosure.scad (the master). If you change a size here, change it in the other print file too.
//
// ratatouille chest box. OpenSCAD, all sizes in mm.
// Open in OpenSCAD (free, macOS). F5 = preview, F6 = render, then File > Export > STL.
//
// Coordinates (as worn): x = left to right, y = front (outside) to back (your chest),
// z = bottom to top. The shell is open at the back. The lid closes it and carries the strap loops.
//
// The dimensions are GUESSES from listings and are deliberately oversized.
// Measure the real parts and change the variables below.

/* [Inside space] */
in_w = 84;              // inside width. Fits 2 boards side by side (2 x 28 + gap + margins)
in_h = 84;              // inside height. Boards are ~64 tall (cam incl. antenna), USB end down,
                        // plus ~17 mm on top for the display wires
in_d = 36;              // inside depth, front wall to lid
wall = 2.4;
lid_t = 2.4;
rebate = 1.2;           // the lid sits in a step this wide, flush with the back
fit = 0.3;              // gap around the lid so it drops in after printing
corner_r = 3;
lid_corner_r = corner_r - (wall - rebate);   // the lid step's corner radius (keeps a 1.2 mm rim at the corners)

/* [Boards, on one carrier perfboard] */
// Both ESPs plug into female headers on a perfboard ("carrier"). The carrier screws onto
// 2 posts sticking back from the front wall, so the camera is held by the same piece
// as the pan-tilt (keeps the aim calibration valid).
board_gap = 36;               // centre-to-centre spacing of the two boards
cam_x = in_w/2 - board_gap/2; // camera board centre, from the inside left wall
s3_x  = in_w/2 + board_gap/2; // S3 centre
post_len = 22;          // front wall to carrier. Lens tip ends ~2 mm behind the front wall.
                        //   (8.5 female header + 1.6 board + ~10 module + camera). Adjust after measuring.
post_r = 3.2;
post_z = [in_h/2 - 27.94, in_h/2 + 27.94];  // 2 posts on the centre column between the boards, 22 holes apart
side_post_dx = 15 * 2.54;   // + 2 posts at mid-height, 15 holes left/right of the centre column (beside the boards)
// All 4 posts sit on the 2.54 mm perfboard grid: a diamond, so the carrier can't rock.
// Carrier: a 7 x 9 cm perfboard turned sideways, cut to ~82 wide x 70 tall, bottom 2 corners nipped
// ~7 x 4 mm (corner pillars). Drill 4 holes to 3.2 mm. Mark them through the posts rather than trusting this.
post_xz = [[in_w/2, post_z[0]], [in_w/2, post_z[1]],
           [in_w/2 - side_post_dx, in_h/2], [in_w/2 + side_post_dx, in_h/2]];
screw_d = 2.5;          // pilot hole for self-tapping M3. Use 4.0 for M3 heat-set inserts.

/* [Camera window] */
// Freenove WROVER CAM: the camera sits on top of the WROVER module, about 34 mm up from the
// USB-C end and ~3 mm towards the 5V pin row (left, seen from the front). Estimated from a photo.
cam_board_z0 = 3;       // bottom (USB) edge of the boards above the inside floor
lens_from_bottom = 34;
cam_lens_dx = -3;
lens_z = cam_board_z0 + lens_from_bottom;
cam_win_w = 16;         // size at the inside face of the wall (oversized so the lens can shift)
cam_win_h = 20;
cam_win_flare = 2.4;    // the opening widens by this much per side towards the outside (= wall, so a 45° chamfer that prints without supports), so the
                        // wall edge doesn't clip the 66 deg view (chamfered window)
cam_win_drop = 4;       // window centre sits this far below the lens, for a lens tilted down 30-45 deg

/* [USB openings and power cable, all in the floor] */
usb_slot_w = 14;        // fits a USB-C plug body (~12 x 7). Boards are 28 wide, so they can't fall through.
usb_slot_y0 = 4;        // slot starts this far behind the front wall's inside face
usb_slot_len = 14;      // ports should land ~7-14 mm back (board face + connector). Shrink after measuring.
cable_x = s3_x;         // power cable notch, under J1 (behind the S3, between its header rows), open to the back
cable_w = 6;
cable_len = 10;

/* [Display, glued on the OUTSIDE of the top, facing up so you can glance down at it] */
disp_on = true;
disp_x = s3_x + 3;      // centre of the pin slot, above the S3 (nudged right, away from the pan-tilt plate)
disp_y = 6;             // slot centre, behind the front wall's inside face
disp_slot = [22, 4];    // fits an 8-pin 2.54 mm header (20.3 long)

/* [Top: pan-tilt mount] */
// Base plate (measured): 38 x 32 x 2 mm, 4 corner holes 2.5 mm at 31.5 x 26.5 centres, plus the
// pan-servo horn screw and 2 small screws sticking out of its underside in the middle.
// Bolts down with M2 bolts + nuts inside the box. Total pan-tilt height ~62 mm.
pt_x = cam_x + 2;       // pan-tilt right above the lens (small parallax); +2 keeps the left nuts clear of the corner pillar
pt_y = in_d/2 - wall/2; // centred front-to-back on the OUTSIDE of the top (inside centre is wall/2 further back)
pt_holes = [31.5, 26.5]; // MEASURED base-plate hole spacing [left-right, front-back]. Plate is 38 x 32 x 2.
pt_hole_d = 3.0;        // the plate's holes are 2.5: M2 (or M2.5) bolts + nuts inside the box
pt_slop = 0.75;         // each hole is a short left-right slot (+-0.75) in case the spacing is a bit off.
                        // Mount the plate with its 38 mm side left-right (the pan servo turns, so facing doesn't matter).
pt_hub_d = 22;          // clearance for the horn screw + the 2 small screws (~8 mm each side of centre)
wire_hole = [12, 10];   // servo + laser wires: hole in the LEFT side wall near the top [length, height].
                        // (On the top it would sit under the display.) Cables drop over the left edge into it.
wire_hole_y = 4;        // from the front wall's inside face
wire_hole_z = in_h - 22;  // bottom of the hole above the inside floor (clear of the vents and corner pillar)

/* [Vents] */
vent_n = 5;

/* [Strap loops and lanyard lugs on the lid] */
strap_w = 53;           // fits a ~50 mm strap (measure it: strap width + 2-3 mm)
strap_t = 6.5;          // gap under each loop bridge: strap ~4-5 mm thick + wiggle room
// Lanyard: two eyelets near the top corners, one per clip of a 2-clip lanyard (lobster clasp / swivel hook).
// The eyelet is a thin ring so the clip's hook can close around it.
lanyard_x = [-30, 30];  // eyelet offsets from the lid centre: two, near the top corners (for a 2-clip lanyard). [0] = one centred
lanyard_hole = 5;       // fits most lanyard clips and split rings
lanyard_t = 4;          // eyelet thickness (what the clip hooks around)
lanyard_ring = 2.5;     // material around the hole

$fn = 40;

OW = in_w + 2*wall;     // outside width
OH = in_h + 2*wall;     // outside height
OD = in_d + wall;       // outside depth (front wall + inside)
boss_r = 4;
boss_len = 9;
bi = boss_r - 1;        // boss centre from the inside walls (sinks 1 mm into the wall so it's attached)
boss_c = [[bi, bi], [in_w - bi, bi], [bi, in_h - bi], [in_w - bi, in_h - bi]];

// cylinder along +y
module ycyl(r, h, r2 = -1) { rotate([-90, 0, 0]) cylinder(r1 = r, r2 = (r2 < 0 ? r : r2), h = h); }

module rounded_block(w, d, h, r) {
    hull() for (x = [r, w - r], z = [r, h - r]) translate([x, 0, z]) ycyl(r, d);
}

module shell() {
    difference() {
        union() {
            difference() {
                rounded_block(OW, OD, OH, corner_r);
                translate([wall, wall, wall]) cube([in_w, OD, in_h]);                 // cavity
            }
            // lid screw bosses: full-length columns in the 4 inside corners, from the front wall to the lid.
            // When printing (front wall on the bed) they grow straight up from the bed: no overhang at all.
            for (c = boss_c) translate([wall + c[0], wall - 0.1, wall + c[1]]) ycyl(boss_r, OD - lid_t - wall + 0.1);
            // carrier posts from the front wall
            for (p = post_xz) translate([wall + p[0], wall - 0.1, wall + p[1]]) ycyl(post_r, post_len + 0.1);
        }
        // step for the lid
        translate([wall - rebate, OD - lid_t, wall - rebate])   // corners rounded to follow the outside, so the rim stays (wall - rebate) thick
            rounded_block(in_w + 2*rebate, lid_t + 1, in_h + 2*rebate, lid_corner_r);
        // screw holes
        for (c = boss_c) translate([wall + c[0], OD - lid_t - boss_len - 1, wall + c[1]]) ycyl(screw_d/2, boss_len + 2);
        for (p = post_xz) translate([wall + p[0], wall + post_len - 8, wall + p[1]]) ycyl(screw_d/2, 9);
        // camera window
        translate([wall + cam_x + cam_lens_dx, 0, wall + lens_z - cam_win_drop]) hull() {
            translate([-cam_win_w/2, wall - 0.01, -cam_win_h/2]) cube([cam_win_w, 1, cam_win_h]);
            translate([-cam_win_w/2 - cam_win_flare, -1, -cam_win_h/2 - cam_win_flare])
                cube([cam_win_w + 2*cam_win_flare, 1, cam_win_h + 2*cam_win_flare]);
        }
        // display pin slot in the top
        if (disp_on) translate([wall + disp_x - disp_slot[0]/2, wall + disp_y - disp_slot[1]/2, OH - wall - 1]) cube([disp_slot[0], disp_slot[1], wall + 2]);
        // USB slots in the floor, under each board
        for (x = [cam_x, s3_x]) translate([wall + x - usb_slot_w/2, wall + usb_slot_y0, -1]) cube([usb_slot_w, usb_slot_len, wall + 2]);
        // power cable notch in the floor, open towards the lid
        translate([wall + cable_x - cable_w/2, OD - cable_len, -1]) cube([cable_w, cable_len + 1, wall + 2]);
        // side vents
        for (sx = [-1, OW - wall - 1], i = [0 : vent_n - 1])
            translate([sx, wall + 8, wall + 24 + i * 8]) cube([wall + 2, 16, 2.5]);
        // pan-tilt: 4 diagonal slots and a centre clearance hole
        translate([wall + pt_x, wall + pt_y, OH - wall - 1]) {
            for (sx = [-1, 1], sy = [-1, 1]) hull() for (dx = [-pt_slop, pt_slop])
                translate([sx * pt_holes[0]/2 + dx, sy * pt_holes[1]/2, 0]) cylinder(d = pt_hole_d, h = wall + 2);
            cylinder(d = pt_hub_d, h = wall + 2);
        }
        // servo + laser wire hole in the left wall
        translate([-1, wall + wire_hole_y, wall + wire_hole_z]) cube([wall + 2, wire_hole[0], wire_hole[1]]);
    }
}


// print orientation: front face on the bed, open back facing up
translate([0, OH, 0]) rotate([90, 0, 0]) shell();
