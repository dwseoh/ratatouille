"""Captions: one at a time, and the next only when the voice has finished the previous one."""
from coach.captions import CaptionTimeline


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def speak(cap, clk, sentences_seconds):
    """Queue sentences and feed their audio like the TTS tap would (generation is faster than playback)."""
    for text, _ in sentences_seconds:
        cap.queue(text)
    for _, secs in sentences_seconds:
        cap.audio_started()
        cap.audio_chunk(secs / 2)
        cap.audio_chunk(secs / 2)
        cap.audio_stopped()


def test_shows_one_sentence_at_a_time_and_advances_only_when_the_voice_finishes_each():
    clk = Clock()
    cap = CaptionTimeline(clk)
    speak(cap, clk, [("First sentence here.", 2.0), ("Second one follows.", 3.0), ("And a third.", 1.5)])
    cap.playback_started()
    assert cap.current() == "First sentence here."
    clk.t += 1.9
    assert cap.current() == "First sentence here."                # still being said, although all audio is generated
    clk.t += 0.2
    assert cap.current() == "Second one follows."
    clk.t += 2.7                                                    # 4.8 s in: second sentence has 0.2 s left
    assert cap.current() == "Second one follows."
    clk.t += 0.4                                                    # 5.2 s in: now the third
    assert cap.current() == "And a third."
    clk.t += 5
    assert cap.current() == "And a third."                         # all done: the last caption stays up
    cap.playback_stopped()
    assert cap.current() == "And a third." and not cap.speaking


def test_a_sentence_whose_audio_is_still_arriving_is_the_current_one():
    clk = Clock()
    cap = CaptionTimeline(clk)
    cap.queue("Slow to generate.")
    cap.queue("Never reached yet.")
    cap.audio_started()
    cap.audio_chunk(1.0)                                            # not closed: more audio coming
    cap.playback_started()
    clk.t += 10
    assert cap.current() == "Slow to generate."


def test_before_playback_the_first_queued_sentence_is_shown():
    cap = CaptionTimeline(Clock())
    assert cap.current() == ""
    cap.queue("Hello there my friend.")
    cap.queue("Second.")
    assert cap.current() == "Hello there my friend."


def test_caption_stays_after_the_voice_stops_then_a_new_turn_replaces_it():
    clk = Clock()
    cap = CaptionTimeline(clk)
    speak(cap, clk, [("Old reply.", 1.0)])
    cap.playback_started()
    clk.t += 2
    cap.playback_stopped()
    assert cap.current() == "Old reply."
    cap.queue("New reply.")
    assert cap.current() == "New reply."


def test_barge_in_drops_the_unspoken_sentences_but_keeps_what_was_on_screen():
    clk = Clock()
    cap = CaptionTimeline(clk)
    speak(cap, clk, [("Being said.", 2.0), ("Never spoken.", 2.0)])
    cap.playback_started()
    clk.t += 0.5
    assert cap.current() == "Being said."
    cap.reset()
    assert cap.current() == "Being said." and not cap.speaking
    clk.t += 10
    assert cap.current() == "Being said."


def test_a_pause_in_the_voice_between_sentences_restarts_the_timeline():
    clk = Clock()
    cap = CaptionTimeline(clk)
    speak(cap, clk, [("Part one.", 1.0)])
    cap.playback_started()
    clk.t += 2
    cap.playback_stopped()                                          # voice went quiet (gap)
    speak(cap, clk, [("Part two.", 1.0), ("Part three.", 1.0)])
    cap.playback_started()
    assert cap.current() == "Part two."
    clk.t += 1.1
    assert cap.current() == "Part three."


def test_blank_sentences_are_ignored():
    cap = CaptionTimeline(Clock())
    cap.queue("   ")
    assert cap.current() == "" and not cap.speaking
