"""Canonical caption alignment: the narration script is the only visible caption text.

Timing providers (ASR today, forced alignment / native TTS later) only decide
*when* canonical tokens are spoken. These tests feed deliberately noisy timing
evidence and require the rendered caption text to stay exactly canonical.
"""

import json
import math
from pathlib import Path

import pytest

from clipforge import alignment
from clipforge.alignment import (
    MIN_COVERAGE,
    align_narration,
    build_caption_track,
    caption_integrity_issues,
    group_aligned_words,
    group_canonical_words,
    map_canonical_timings,
)
from clipforge.caption_text import canonical_tokens, spoken_units
from clipforge.config import Settings
from clipforge.renderer import _write_ass_captions


def settings(**updates) -> Settings:
    values = {"clipforge_ai_mode": "local", "openai_api_key": None, "caption_alignment_provider": "auto"}
    values.update(updates)
    return Settings(**values)


def heard(*words: str, step: float = 0.4, gap: float = 0.0, start: float = 0.0) -> list[dict]:
    """Evidence for consecutive, evenly spaced words."""
    evidence = []
    cursor = start
    for word in words:
        evidence.append({"text": word, "start": round(cursor, 3), "end": round(cursor + step, 3)})
        cursor += step + gap
    return evidence


class FakeAligner:
    name = "fake_asr"
    model_name = "fake-v1"

    def __init__(self, evidence: list[dict] | None = None, *, error: Exception | None = None):
        self.evidence = evidence or []
        self.error = error
        self.calls = 0

    def align(self, _audio, _language):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [dict(word) for word in self.evidence]


def track(script, evidence, *, language="de", seconds=None, group=4, **kwargs):
    seconds = seconds if seconds is not None else max(1.0, max((w["end"] for w in evidence), default=1.0) + 0.5)
    return build_caption_track(
        Path("/nonexistent/voice.wav"),
        script,
        seconds,
        language,
        settings(),
        words_per_group=group,
        audio_seconds=seconds,
        aligner=kwargs.pop("aligner", None) or FakeAligner(evidence),
        **kwargs,
    )


def caption_text(result) -> str:
    return " ".join(item["text"] for item in result.items)


def displayed_words(result) -> list[str]:
    return [word["text"] for item in result.items for word in item["words"]]


# ---------------------------------------------------------------------------
# The required semantic regression


def test_recognised_gap_never_replaces_canonical_gelb():
    result = track("Der Himmel wird gelb.", heard("Der", "Himmel", "wird", "Gap."))

    assert result.timing == "word_aligned"
    assert caption_text(result) == "Der Himmel wird gelb."
    assert displayed_words(result) == ["Der", "Himmel", "wird", "gelb."]
    gelb = result.items[-1]["words"][-1]
    # "gelb" is timed by the acoustic slot the recogniser called "Gap".
    assert (gelb["start"], gelb["end"]) == (1.2, 1.6)
    serialised = json.dumps({"items": result.items, "report": result.report}, ensure_ascii=False)
    assert "Gap" not in serialised and "gap" not in serialised
    assert result.report["text_source"] == "canonical_script"
    assert result.report["timing_source"] == "recognised_word_timing"


def test_rendered_ass_subtitles_show_canonical_word(tmp_path):
    result = track("Der Himmel wird gelb.", heard("Der", "Himmel", "wird", "Gap."))
    state = {
        "timeline": {"width": 1080, "height": 1920},
        "captions": {"enabled": True, "style": "karaoke", "items": result.items},
    }
    output = _write_ass_captions(state, 2.0, tmp_path).read_text(encoding="utf-8")

    assert "gelb." in output
    assert "Gap" not in output


def test_align_narration_words_are_canonical_display_tokens(tmp_path):
    result = align_narration(
        tmp_path / "voice.wav",
        "Der Himmel wird gelb.",
        2.0,
        "de",
        settings(),
        aligner=FakeAligner(heard("der", "himmel", "wird", "gap")),
    )
    assert result.status == "word_aligned"
    assert [word["text"] for word in result.words] == ["Der", "Himmel", "wird", "gelb."]


# ---------------------------------------------------------------------------
# Language-aware canonical text


def test_punctuation_and_capitalisation_come_from_the_script():
    script = "Warum? Der Mars, so heißt es, ist rot!"
    result = track(script, heard("warum", "DER", "mars", "so", "HEISST", "es", "ist", "rot"))

    assert caption_text(result) == "Warum? Der Mars, so heißt es, ist rot!"
    assert result.items[0]["text"] == "Warum?"  # sentence ends stay hard boundaries


def test_repeated_words_keep_order_when_one_occurrence_is_missing():
    script = "Das ist das, was das Team immer wieder sagt und das ist wahr."
    evidence = heard("Das", "ist", "das", "was", "Team", "immer", "wieder", "sagt", "und", "das", "ist", "wahr", gap=0.1)
    # The third "das" was not recognised at all.
    result = track(script, evidence, group=3)

    assert displayed_words(result) == [token.display for token in canonical_tokens(script, "de")]
    words = [word for item in result.items for word in item["words"]]
    assert words[4]["text"] == "das" and words[4]["status"] == "interpolated"
    starts = [word["start"] for word in words]
    assert starts == sorted(starts)


def test_umlauts_and_sharp_s_are_preserved_even_if_recognised_without_them():
    script = "Die Größe der Straße überrascht Jürgen."
    result = track(script, heard("Die", "Grösse", "der", "Strasse", "uberrascht", "Jurgen."))

    assert caption_text(result) == script
    assert spoken_units("Straße", "de") == ["straße"]


def test_decimal_comma_spans_every_spoken_unit():
    script = "Es sind 2,5 Milliarden Sterne."
    result = track(script, heard("Es", "sind", "zwei", "Komma", "fünf", "Milliarden", "Sterne."))

    words = [word for item in result.items for word in item["words"]]
    decimal = next(word for word in words if word["text"] == "2,5")
    assert (decimal["start"], decimal["end"]) == (0.8, 2.0)
    assert caption_text(result) == script


def test_percent_acronym_and_hyphenated_compound_stay_one_canonical_token_each():
    script = "20 % der NASA-Daten stammen aus Messungen."
    result = track(script, heard("20%", "der", "NASA", "Daten", "stammen", "aus", "Messungen."), group=8)

    assert displayed_words(result) == ["20 %", "der", "NASA-Daten", "stammen", "aus", "Messungen."]
    nasa = result.items[0]["words"][2]
    assert (nasa["start"], nasa["end"]) == (0.8, 1.6)  # two acoustic units -> one canonical token


def test_spelled_acronym_and_abbreviation_are_matched_in_spoken_form():
    script = "Die NASA misst z. B. den Staub auf dem Mars."
    evidence = heard("Die", "N.A.S.A.", "misst", "zum", "Beispiel", "den", "Staub", "auf", "dem", "Mars.")
    result = track(script, evidence, group=8)

    assert displayed_words(result) == ["Die", "NASA", "misst", "z. B.", "den", "Staub", "auf", "dem", "Mars."]
    # "z. B." is not a sentence end, so it does not split the caption group.
    assert [item["text"] for item in result.items] == ["Die NASA misst z. B. den Staub auf dem", "Mars."]


def test_years_and_numbers_in_english_and_german():
    english = track("It began in 1999 with 3 probes.", heard("It", "began", "in", "nineteen", "ninety-nine", "with", "three", "probes."), language="en")
    german = track("Im Jahr 2026 starten 12 Sonden.", heard("Im", "Jahr", "2026", "starten", "zwölf", "Sonden."))

    assert caption_text(english) == "It began in 1999 with 3 probes."
    assert english.report["lexical_agreement"] == 1.0
    assert caption_text(german) == "Im Jahr 2026 starten 12 Sonden."
    assert german.report["lexical_agreement"] == 1.0


def test_english_contractions_decimals_and_hyphens():
    script = "Don't panic: 2.5% of well-known stars aren't stable."
    evidence = heard("Don't", "panic", "two", "point", "five", "percent", "of", "well", "known", "stars", "arent", "stable.")
    result = track(script, evidence, language="en", group=8)

    assert caption_text(result) == script
    assert result.report["lexical_agreement"] == 1.0


def test_one_canonical_token_split_into_several_acoustic_units():
    result = track("Unser Sonnensystem ist alt.", heard("Unser", "Sonnen", "system", "ist", "alt."))

    sonnensystem = result.items[0]["words"][1]
    assert sonnensystem["text"] == "Sonnensystem"
    assert (sonnensystem["start"], sonnensystem["end"]) == (0.4, 1.2)


def test_several_canonical_tokens_in_one_acoustic_unit_are_split_proportionally():
    script = "Wir gehen heute Abend los."
    evidence = [
        {"text": "Wir", "start": 0.0, "end": 0.4},
        {"text": "gehen", "start": 0.4, "end": 1.0},
        {"text": "heuteabend", "start": 1.0, "end": 2.0},
        {"text": "los.", "start": 2.0, "end": 2.4},
    ]
    words = [word for item in track(script, evidence, group=8).items for word in item["words"]]

    assert (words[2]["text"], words[2]["start"], words[2]["end"]) == ("heute", 1.0, 1.5)
    assert (words[3]["text"], words[3]["start"], words[3]["end"]) == ("Abend", 1.5, 2.0)


# ---------------------------------------------------------------------------
# Partial alignment repair

TEN = "Eins zwei drei vier fünf sechs sieben acht neun zehn."
TEN_WORDS = ["Eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn."]


def test_missing_middle_word_is_interpolated_inside_its_neighbours():
    evidence = [word for word in heard(*TEN_WORDS, gap=0.1) if word["text"] != "fünf"]
    result = track(TEN, evidence, group=8)
    words = [word for item in result.items for word in item["words"]]

    assert result.timing == "word_aligned"
    assert words[4]["text"] == "fünf" and words[4]["status"] == "interpolated"
    assert words[3]["end"] <= words[4]["start"] < words[4]["end"] <= words[5]["start"]
    assert result.report["interpolated_tokens"] == 1
    assert result.report["coverage"] == 0.9


def test_missing_middle_word_without_a_gap_shares_the_neighbours_window():
    evidence = [word for word in heard(*TEN_WORDS) if word["text"] != "fünf"]
    # Remove the hole the missing word left: the recogniser ran its neighbours together.
    for word in evidence[4:]:
        word["start"] = round(word["start"] - 0.4, 3)
        word["end"] = round(word["end"] - 0.4, 3)
    words = [word for item in track(TEN, evidence, group=8).items for word in item["words"]]

    assert [word["text"] for word in words] == TEN_WORDS
    assert words[3]["start"] < words[4]["start"] < words[4]["end"] <= words[5]["start"] + 1e-6
    assert all(word["end"] > word["start"] for word in words)


def test_missing_first_word_gets_a_bounded_estimate_before_its_neighbour():
    evidence = heard(*TEN_WORDS[1:], start=1.0)
    words = [word for item in track(TEN, evidence, group=8).items for word in item["words"]]

    assert words[0]["text"] == "Eins" and words[0]["status"] == "interpolated"
    assert 0.0 <= words[0]["start"] < words[0]["end"] == words[1]["start"] == 1.0
    assert words[0]["end"] - words[0]["start"] < 1.0  # not stretched over the leading silence


def test_missing_final_word_is_estimated_after_its_neighbour_within_the_audio():
    evidence = heard(*TEN_WORDS[:-1])
    result = track(TEN, evidence, group=8, seconds=4.0)
    last = result.items[-1]["words"][-1]

    assert last["text"] == "zehn." and last["status"] == "interpolated"
    assert last["start"] == pytest.approx(3.6) and last["start"] < last["end"] <= 4.0


def test_low_coverage_falls_back_to_canonical_phrase_timing():
    evidence = heard("Eins", "zwei", "drei", "neun", "zehn.")
    result = track(TEN, evidence)

    assert result.timing == "phrase_fallback"
    assert result.provider == "estimated_segments"
    assert all(item["timing"] == "phrase_estimate" for item in result.items)
    assert caption_text(result) == TEN
    assert result.report["timing_source"] == "phrase_fallback"
    assert "could not be timed" in result.report["fallback_reason"]


def test_evidence_that_disagrees_with_the_script_is_not_trusted():
    result = track("Der Himmel wird gelb.", heard("Ganz", "andere", "Worte", "hier."))

    assert result.timing == "phrase_fallback"
    assert caption_text(result) == "Der Himmel wird gelb."
    assert "matches only" in result.report["fallback_reason"]


def test_backwards_timestamp_is_repaired_or_rejected_never_rendered_out_of_order():
    evidence = heard(*TEN_WORDS)
    evidence[5] = {"text": "sechs", "start": 0.1, "end": 0.3}  # jumps back in time
    result = track(TEN, evidence, group=8)
    words = [word for item in result.items for word in item["words"]]

    assert result.timing == "word_aligned"
    assert words[5]["status"] == "interpolated"
    assert [word["start"] for word in words] == sorted(word["start"] for word in words)


def test_out_of_range_and_non_finite_timestamps_are_never_used():
    evidence = heard(*TEN_WORDS)
    evidence[2] = {"text": "drei", "start": math.nan, "end": 0.9}
    evidence[9] = {"text": "zehn.", "start": 30.0, "end": 31.0}
    result = track(TEN, evidence, group=8, seconds=4.0)
    words = [word for item in result.items for word in item["words"]]

    assert words[2]["status"] == "interpolated" and words[9]["status"] == "interpolated"
    assert all(0 <= word["start"] < word["end"] <= 4.0 for word in words)


def test_many_out_of_range_timestamps_fall_back():
    evidence = heard(*TEN_WORDS, start=20.0)
    result = track(TEN, evidence, seconds=4.0)

    assert result.timing == "phrase_fallback"


def test_provider_failure_and_missing_provider_fall_back_with_diagnostics(tmp_path):
    failed = track("Der Himmel wird gelb.", [], aligner=FakeAligner(error=RuntimeError("offline")))
    disabled = build_caption_track(
        tmp_path / "voice.wav", "Der Himmel wird gelb.", 2.0, "de",
        settings(caption_alignment_provider="none"), words_per_group=4,
    )

    assert failed.timing == "phrase_fallback" and "failed" in failed.diagnostic.lower()
    assert "RuntimeError" in failed.report["fallback_reason"]
    assert disabled.timing == "phrase_fallback" and "disabled" in disabled.diagnostic.lower()
    assert caption_text(disabled) == "Der Himmel wird gelb."


# ---------------------------------------------------------------------------
# Integrity gate


def _good_items(script="Der Himmel wird gelb."):
    words, _metrics, issues = map_canonical_timings(
        canonical_tokens(script, "de"), heard("Der", "Himmel", "wird", "gelb."), "de", 2.0
    )
    assert not issues
    return group_canonical_words(words, 4)


def test_integrity_gate_accepts_canonical_captions():
    assert caption_integrity_issues(_good_items(), "Der Himmel wird gelb.", "de", 2.0) == []


def test_integrity_gate_rejects_recogniser_only_text():
    items = _good_items()
    items[0]["words"][3]["text"] = "Gap."
    items[0]["text"] = "Der Himmel wird Gap."
    issues = caption_integrity_issues(items, "Der Himmel wird gelb.", "de", 2.0)

    assert any("differ from the canonical" in issue for issue in issues)
    assert any("reconstruct" in issue for issue in issues)


def test_integrity_gate_rejects_reordered_tokens_and_bad_times():
    items = _good_items()
    words = items[0]["words"]
    words[1], words[2] = words[2], words[1]
    items[0]["text"] = " ".join(word["text"] for word in words)
    assert any("order" in issue for issue in caption_integrity_issues(items, "Der Himmel wird gelb.", "de", 2.0))

    items = _good_items()
    items[0]["words"][2]["start"] = 0.1
    assert any("monotonic" in issue for issue in caption_integrity_issues(items, "Der Himmel wird gelb.", "de", 2.0))

    items = _good_items()
    items[0]["words"][3]["end"] = 9.0
    items[0]["end"] = 9.0
    assert any("after the narration" in issue for issue in caption_integrity_issues(items, "Der Himmel wird gelb.", "de", 2.0))

    items = _good_items()
    items[0]["words"][0]["start"] = math.inf
    assert any("non-finite" in issue for issue in caption_integrity_issues(items, "Der Himmel wird gelb.", "de", 2.0))

    items = _good_items()
    items[0]["words"][1]["end"] = items[0]["words"][1]["start"]
    assert any("ends before" in issue for issue in caption_integrity_issues(items, "Der Himmel wird gelb.", "de", 2.0))


def test_integrity_gate_enforces_coverage_from_the_report():
    issues = caption_integrity_issues(
        _good_items(), "Der Himmel wird gelb.", "de", 2.0, {"coverage": MIN_COVERAGE - 0.01, "lexical_agreement": 1}
    )
    assert any("coverage" in issue for issue in issues)


def test_failed_integrity_gate_renders_phrase_fallback(monkeypatch):
    monkeypatch.setattr(alignment, "caption_integrity_issues", lambda *args, **kwargs: ["forced failure"])
    result = track("Der Himmel wird gelb.", heard("Der", "Himmel", "wird", "Gap."))

    assert result.timing == "phrase_fallback"
    assert "integrity" in result.diagnostic.lower()
    assert result.report["integrity"] == {"passed": False, "issues": ["forced failure"]}
    assert caption_text(result) == "Der Himmel wird gelb."


# ---------------------------------------------------------------------------
# Grouping, providers and cache


def test_grouping_is_deterministic_and_uses_word_timings():
    script = "Mars ist rot. Sein Staub enthält Eisenoxid und färbt den Himmel."
    evidence = heard("Mars", "ist", "rot.", "Sein", "Staub", "enthält", "Eisenoxid", "und", "färbt", "den", "Himmel.", gap=0.05)
    first, second = track(script, evidence, group=3), track(script, evidence, group=3)

    assert first.items == second.items
    assert [item["text"] for item in first.items] == ["Mars ist rot.", "Sein Staub enthält", "Eisenoxid und färbt", "den Himmel."]
    for item in first.items:
        assert item["start"] == item["words"][0]["start"]
        assert item["end"] == item["words"][-1]["end"]


def test_native_tts_timings_take_priority_over_the_provider():
    aligner = FakeAligner(heard("Der", "Himmel", "wird", "Gap."))
    result = track("Der Himmel wird gelb.", [], aligner=aligner, seconds=2.0, native_timings=heard("Der", "Himmel", "wird", "gelb."))

    assert aligner.calls == 0
    assert result.report["timing_source"] == "native_tts"
    assert caption_text(result) == "Der Himmel wird gelb."


def test_known_transcript_aligner_receives_canonical_spoken_units():
    class ForcedAligner:
        name = "fake_ctc"
        model_name = "ctc-v1"
        timing_source = "forced_alignment"

        def __init__(self):
            self.units = None

        def align(self, _audio, _language):
            raise AssertionError("a known-transcript aligner must not transcribe")

        def align_transcript(self, _audio, units, _language):
            self.units = units
            return heard(*(" ".join(unit) for unit in units))

    forced = ForcedAligner()
    result = track("Es sind 20 % mehr.", [], aligner=forced, seconds=3.0)

    assert forced.units == [["es"], ["sind"], ["zwanzig", "prozent"], ["mehr"]]
    assert result.report["timing_source"] == "forced_alignment"
    assert caption_text(result) == "Es sind 20 % mehr."


def test_alignment_cache_is_reused_and_invalidated_by_its_inputs(tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF" + b"\x01" * 5000)
    cache = tmp_path / "alignment"
    aligner = FakeAligner(heard("Der", "Himmel", "wird", "Gap."))

    def run(script="Der Himmel wird gelb.", language="de", provider=aligner):
        return build_caption_track(
            audio, script, 2.0, language, settings(), words_per_group=4,
            audio_seconds=2.0, cache_dir=cache, aligner=provider,
        )

    first = run()
    second = run()
    assert (first.report["cache"], second.report["cache"]) == ("miss", "hit")
    assert aligner.calls == 1
    assert first.items == second.items
    stored = "".join(path.read_text(encoding="utf-8") for path in cache.glob("*.json"))
    assert "gelb." in stored and "Gap" not in stored

    run(script="Der Himmel wird blau.")  # script changed
    assert aligner.calls == 2
    run(language="en")  # language changed
    assert aligner.calls == 3
    other_model = FakeAligner(heard("Der", "Himmel", "wird", "gelb."))
    other_model.model_name = "fake-v2"
    run(provider=other_model)  # provider model changed
    assert other_model.calls == 1
    audio.write_bytes(b"RIFF" + b"\x02" * 5000)  # narration audio changed
    run()
    assert aligner.calls == 4


def test_stale_cache_with_different_tokens_is_ignored(tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF" + b"\x01" * 5000)
    cache = tmp_path / "alignment"
    aligner = FakeAligner(heard("Der", "Himmel", "wird", "gelb."))
    build_caption_track(audio, "Der Himmel wird gelb.", 2.0, "de", settings(), words_per_group=4, cache_dir=cache, aligner=aligner)
    for path in cache.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["words"][3]["text"] = "Gap."
        path.write_text(json.dumps(payload), encoding="utf-8")

    result = build_caption_track(audio, "Der Himmel wird gelb.", 2.0, "de", settings(), words_per_group=4, cache_dir=cache, aligner=aligner)
    assert aligner.calls == 2
    assert caption_text(result) == "Der Himmel wird gelb."


def test_legacy_grouping_with_script_uses_canonical_words():
    groups = group_aligned_words(heard("Der", "Himmel", "wird", "Gap"), 4, "Der Himmel wird gelb.", "de")
    assert groups[0]["text"] == "Der Himmel wird gelb."


# ---------------------------------------------------------------------------
# Spoken normalisation


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("2,5", "de", ["zwei", "komma", "fünf"]),
        ("1.000.000", "de", ["eine", "million"]),
        ("2026", "de", ["zweitausendsechsundzwanzig"]),
        ("1999", "de", ["neunzehnhundertneunundneunzig"]),
        ("21", "de", ["einundzwanzig"]),
        ("20 %", "de", ["zwanzig", "prozent"]),
        ("z. B.", "de", ["zum", "beispiel"]),
        ("NASA-Daten", "de", ["nasa", "daten"]),
        ("2.5", "en", ["two", "point", "five"]),
        ("1,250", "en", ["one", "thousand", "two", "hundred", "fifty"]),
        ("2026", "en", ["twenty", "twenty", "six"]),
        ("1905", "en", ["nineteen", "oh", "five"]),
        ("$5", "en", ["five", "dollars"]),
        ("3rd", "en", ["third"]),
        ("1990s", "en", ["nineteen", "nineties"]),
        ("e.g.", "en", ["for", "example"]),
        ("-4", "en", ["minus", "four"]),
        ("Apollo-11", "en", ["apollo", "eleven"]),
    ],
)
def test_spoken_normalisation(text, language, expected):
    assert spoken_units(text, language) == expected


def test_canonical_tokens_cover_every_visible_character():
    script = "„Wow – 20 % mehr!“ Das ist z. B. 2,5-mal so viel… oder?"
    tokens = canonical_tokens(script, "de")

    assert "".join(token.display for token in tokens).replace(" ", "") == script.replace(" ", "")
    assert [token.display for token in tokens][:3] == ["„Wow –", "20 %", "mehr!“"]
    assert [token.sentence_end for token in tokens] == [False, False, True, False, False, False, False, False, True, True]
