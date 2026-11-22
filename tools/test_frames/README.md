# Test photos

`tools/live_agent_check.py`, `tools/live_watcher_check.py` and `tools/bench_llm.py` send these to the real
Claude API in place of the camera. They need three photos of your own, 640x480 or smaller, named:

| File | Shows |
|---|---|
| `avocado_hold.jpg` | a whole avocado on the cutting board (step 1 not done yet) |
| `avocado_halves.jpg` | avocado halves, cut faces up (step 1 done) |
| `mash_bowl.jpg` | mashed avocado in a bowl (the mash step done) |

`avocado_hold.jpg` is a frame from our chest camera. Take the other two while cooking: record a short scene
with `cd laptop && python -m tools.capture_frames halves --present avocado`, then copy the best frame from
`tools/eval_frames/` to this folder under the right name. The checks stop and name any missing photo.
