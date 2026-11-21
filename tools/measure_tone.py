"""Measure how the coach sounds: reply length (words, sentences) and how often it sounds warm/encouraging.

    .venv/bin/python tools/measure_tone.py            # ~25 real turns, a few cents

Use it before and after changing the SYSTEM prompt's tone. Three kinds of moment: neutral questions, progress
("I cut it"), and struggle ("I made a mess"). We want: warm on progress and struggle, still short everywhere.
"""
import asyncio
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "laptop"))

import live_agent_check as L  # noqa: E402

NEUTRAL = ["Where do I start?", "Where's the knife?", "How big should the pieces be?", "What's next?", "Which one is the lime?"]
PROGRESS = ["I cut the avocados in half.", "Okay, I mashed it.", "I think I did it right, I diced the onion.", "Done, all mixed.",
            "I got the pits out."]
STRUGGLE = ["Ugh, I made a mess.", "I'm not sure I'm doing this right.", "The avocado slipped and I can't get the pit out.",
            "This is harder than I thought.", "I don't know if I can do this."]
WARM = re.compile(r"\b(nice|great|good|perfect|well done|awesome|excellent|lovely|that's right|nicely|you've got this|you got this|"
                  r"no worries|don't worry|it happens|easy does it|take your time|no problem|you can do|happens to everyone|"
                  r"that's okay|that's ok|doing well|going well|looks right|looks good|looking good|nailed)\b", re.I)


async def run_group(label, questions, step=0):
    agent, st, aimer, _ = L.build(step=step)
    agent.first_token_timeout = 30
    words, sents, warm = [], [], 0
    shown = []
    for q in questions:
        sink = L.Sink()
        await agent.turn(q, L.frames("avocado_halves"), sink)
        text = " ".join(sink.sentences)
        words.append(len(text.split()))
        sents.append(len(sink.sentences))
        warm += bool(WARM.search(text))
        shown.append(f"    {q!r} -> {text}")
    print(f"{label:9} words/reply {statistics.mean(words):5.1f} (max {max(words)})  sentences {statistics.mean(sents):.1f}  "
          f"warm {warm}/{len(questions)}")
    return words, sents, warm, shown


async def main():
    show = "--show" in sys.argv
    allw, allwarm = [], 0
    for label, qs, step in (("neutral", NEUTRAL, 0), ("progress", PROGRESS, 2), ("struggle", STRUGGLE, 1)):
        words, sents, warm, shown = await run_group(label, qs, step)
        allw += words
        if show:
            print("\\n".join(shown))
    print(f"ALL       words/reply {statistics.mean(allw):5.1f}")


asyncio.run(main())
