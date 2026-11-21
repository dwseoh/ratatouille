"""Laser dot tracking (stretch, off by default): the blink check + DotTracker, the writer of State.dot.

find_red_dot (vision.py) finds dot-like blobs, but a white highlight on a tomato looks the same. So a NEW dot is
only trusted after a blink: laser off for a moment, and if the blob is gone in the OFF frame, it was the laser.
After that the dot is tracked frame to frame (nearest blob within TRACK_PX) with no more blinks until it jumps.

Wiring, only when closed-loop aiming is wanted (v1 doesn't run any of this):
    tracker = DotTracker(frames, link, state)
    asyncio.create_task(tracker.run())
    aimer = Aimer(link, calib, state, closed_loop=True)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import cv2
import numpy as np

from .contracts import ControlLinkAPI, FrameSource, State
from .vision import find_red_dot

log = logging.getLogger(__name__)

LAG_S = 0.25            # laser command -> visible in a received frame (UDP + camera + WiFi). Also the blink length.
TRACK_PX = 50           # a blob within this of the last dot is the same dot (no blink needed)
STABLE_PX = 6           # a new blob must stay within this ...
STABLE_N = 2            # ... for this many frames before we blink (no blinking while the laser slews)
MISS_N = 3              # this many frames in a row without the dot -> forget it
BLINK_EVERY_S = 0.6     # at least this long between blinks (each one is a visible flicker)
BLINK_PX = 6            # OFF frame: a blob within this of the ON dot = it didn't go away = not the laser
BLINK_DROP = 40         # ... and the spot must get at least this much darker with the laser off (V, 7x7 mean)


def _dist(a, b) -> float:
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


def _patch_v(frame, x, y, r: int = 3) -> float:
    """Mean brightness (max channel) of the (2r+1)^2 patch around (x, y)."""
    h, w = frame.shape[:2]
    x, y = int(round(x)), int(round(y))
    p = frame[max(y - r, 0):min(y + r + 1, h), max(x - r, 0):min(x + r + 1, w)]
    return float(p.max(axis=2).mean()) if p.size else 0.0


def _brightened(frame_on, frame_off) -> tuple[float, float] | None:
    """Where the ON frame got brightest compared to the OFF frame (= where the laser was), or None."""
    b, g, r = cv2.split(cv2.subtract(frame_on, frame_off))
    m = cv2.GaussianBlur(cv2.max(cv2.max(b, g), r), (5, 5), 0)
    _, peak, _, loc = cv2.minMaxLoc(m)
    return (float(loc[0]), float(loc[1])) if peak >= BLINK_DROP / 4 else None


def blink_check(frame_on, frame_off, near=None) -> tuple[float, float] | None:
    """The dot in the laser-ON frame if it's gone in the laser-OFF frame, else None.

    Looks at the blob nearest `near`; without `near`, nearest to where the ON frame is brightest over the OFF one
    (so a tomato highlight elsewhere in the frame can't win). Gone = no dot-like blob within BLINK_PX in the OFF
    frame AND that spot got >= BLINK_DROP darker. The frames should be close in time (the wearer moves).
    """
    if frame_on is None or frame_off is None or frame_on.shape != frame_off.shape:
        return None
    if near is None:
        near = _brightened(frame_on, frame_off)
        if near is None:
            return None
    p = find_red_dot(frame_on, near)
    if p is None:
        return None
    q = find_red_dot(frame_off, p)
    if q is not None and _dist(p, q) <= BLINK_PX:
        return None                     # still there with the laser off: red food, a highlight
    if _patch_v(frame_on, *p) - _patch_v(frame_off, *p) < BLINK_DROP:
        return None                     # didn't get darker: not our laser
    return p


async def blink_dot(link: ControlLinkAPI, frames: FrameSource, near=None, lag_s: float = LAG_S,
                    sleep: Callable[[float], Awaitable] = asyncio.sleep) -> tuple[float, float] | None:
    """Blink once (laser off for lag_s, then back on) and return the confirmed dot or None.

    For tools that own the laser (calibrate --auto). The laser must already have been on and still for >= lag_s,
    and after this returns it takes another lag_s before frames show it on again.
    """
    on, _ = frames.latest()
    link.laser = False
    await sleep(lag_s)
    off, _ = frames.latest()
    link.laser = True
    return blink_check(on, off, near)


class DotTracker:
    """Writes State.dot: None until a dot passes a blink check, then tracked frame to frame.

    tick() is sync and cheap (one find_red_dot per new frame, ~1-2 ms); run() calls it at `hz`. `clock` must match
    the frame timestamps (FrameSource.latest() uses time.time()). The laser belongs to the Aimer: a blink only
    switches it off briefly and puts it back on only if `want_laser()` (default: State.target is set, since
    laser_off() clears it). If something turns the laser on mid-blink, that blink is dropped.
    """

    def __init__(self, frames: FrameSource, link: ControlLinkAPI, state: State, hz: float = 30.0,
                 clock: Callable[[], float] = time.time, want_laser: Callable[[], bool] | None = None):
        self.frames, self.link, self.state, self.hz, self.clock = frames, link, state, hz, clock
        self.want_laser = want_laser or (lambda: state.target is not None)
        self.blinks = self.confirmed = 0        # counters, for the dashboard / debugging
        self._dot: tuple[float, float] | None = None    # trusted dot
        self._cand = self._cand_frame = None    # untrusted blob waiting for a blink, and the frame it's in
        self._stable = self._misses = 0
        self._last_t = 0.0                      # timestamp of the last frame looked at
        self._blind_until = 0.0                 # frames received before this may still show the old laser state
        self._blink_t0: float | None = None     # when the laser went off for the current blink
        self._last_blink = float("-inf")
        self._laser_was = False

    @property
    def dot(self) -> tuple[float, float] | None:
        return self._dot

    async def run(self) -> None:
        period = 1.0 / self.hz
        while True:
            try:
                self.tick()
            except Exception:       # never let one bad frame kill the loop
                log.exception("dot tracker tick failed")
            await asyncio.sleep(period)

    def tick(self) -> None:
        now = self.clock()
        if self._blink_t0 is not None:
            self._blink_tick(now)
            return
        laser = bool(self.link.laser)
        if laser and not self._laser_was:
            self._blind_until = now + LAG_S     # just switched on: frames still show it off for a moment
        self._laser_was = laser
        if not laser:
            self._forget()
            return
        frame, t = self.frames.latest()
        if frame is None or t <= self._last_t:
            return                              # no new frame
        self._last_t = t
        if t < self._blind_until:
            return
        p = find_red_dot(frame, self._dot or self._cand)
        if p is None:
            self._misses += 1
            if self._misses >= MISS_N:
                self._forget()
            return
        self._misses = 0
        if self._dot is not None and _dist(p, self._dot) <= TRACK_PX:
            self._set(p)                        # same dot, moved a bit
            return
        # A new blob, or the dot jumped: don't trust it until a blink says it's the laser.
        self._set(None)
        near = self._cand is not None and _dist(p, self._cand) <= STABLE_PX
        self._stable = self._stable + 1 if near else 1
        self._cand, self._cand_frame = p, frame
        if self._stable >= STABLE_N and now - self._last_blink >= BLINK_EVERY_S:
            self.link.laser = False
            self._blink_t0 = now
            self.blinks += 1

    def _blink_tick(self, now: float) -> None:
        t0 = self._blink_t0
        if self.link.laser:                     # someone switched it on mid-blink (a new point_at): result unknown
            self._end_blink(now)
            return
        if now - t0 < LAG_S:
            return
        frame, t = self.frames.latest()
        if (frame is None or t < t0 + LAG_S) and now - t0 < 3 * LAG_S:
            return                              # wait for a frame taken with the laser off
        on_frame, cand = self._cand_frame, self._cand
        if self.want_laser():
            self.link.laser = True
        self._end_blink(now)
        if self.link.laser and frame is not None and t >= t0 + LAG_S:
            # the blob we blinked for, or else wherever the laser really is (the candidate was a tomato highlight)
            p = blink_check(on_frame, frame, cand) or blink_check(on_frame, frame)
            if p is not None:
                self._set(p)
                self.confirmed += 1
            else:
                log.debug("blink check failed at %s: not the laser", cand)

    def _end_blink(self, now: float) -> None:
        self._blink_t0 = None
        self._last_blink = now
        self._blind_until = now + LAG_S
        self._laser_was = bool(self.link.laser)
        self._cand = self._cand_frame = None
        self._stable = 0

    def _set(self, p) -> None:
        self._dot = p
        self.state.dot = p

    def _forget(self) -> None:
        self._set(None)
        self._cand = self._cand_frame = None
        self._stable = self._misses = 0
