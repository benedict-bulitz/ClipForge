from clipforge.alignment import group_aligned_words, phrase_fallback_items


def _words(*entries):
    return [
        {"text": text, "start": start, "end": end}
        for text, start, end in entries
    ]


def test_aligned_captions_make_sentence_endings_hard_boundaries_and_keep_pause():
    captions = group_aligned_words(
        _words(
            ("Wir", 3.50, 3.72),
            ("mischen", 3.72, 4.20),
            (".", 4.20, 4.20),
            ("Für", 4.65, 4.86),
            ("dich", 4.86, 5.10),
            ("!", 5.10, 5.10),
            ("Jetzt", 5.55, 5.80),
            ("wirklich", 5.80, 6.18),
            ("?", 6.18, 6.18),
            ("Das", 6.62, 6.85),
            ("passiert", 6.85, 7.18),
            (".", 7.18, 7.18),
        ),
        2,
        "Wir mischen. Für dich! Jetzt wirklich? Das passiert.",
    )

    assert [caption["text"] for caption in captions] == [
        "Wir mischen.",
        "Für dich!",
        "Jetzt wirklich?",
        "Das passiert.",
    ]
    assert [(caption["start"], caption["end"]) for caption in captions] == [
        (3.50, 4.20),
        (4.65, 5.10),
        (5.55, 6.18),
        (6.62, 7.18),
    ]
    assert all(caption["start"] >= 4.65 for caption in captions[1:2])
    assert all(caption["text"].strip(".!?…") != "" for caption in captions)


def test_canonical_script_restores_lost_sentence_punctuation_without_crossing_boundary():
    captions = group_aligned_words(
        _words(
            ("mischen", 0.00, 0.40),
            ("Für", 0.90, 1.10),
            ("dich", 1.10, 1.30),
        ),
        2,
        "mischen. Für dich.",
    )

    assert [caption["text"] for caption in captions] == ["mischen.", "Für dich."]
    assert captions[0]["end"] == 0.40
    assert captions[1]["start"] == 0.90


def test_commas_are_not_hard_caption_boundaries_and_two_word_style_remains():
    captions = group_aligned_words(
        _words(
            ("Wenn", 0.0, 0.2),
            ("es", 0.2, 0.4),
            ("kalt", 0.4, 0.6),
            ("ist,", 0.6, 0.8),
            ("ziehen", 0.8, 1.0),
            ("sich", 1.0, 1.2),
            ("die", 1.2, 1.4),
            ("Muskeln", 1.4, 1.6),
            ("zusammen.", 1.6, 1.9),
        ),
        2,
        "Wenn es kalt ist, ziehen sich die Muskeln zusammen.",
    )

    assert [caption["text"] for caption in captions] == [
        "Wenn es",
        "kalt ist,",
        "ziehen sich",
        "die Muskeln",
        "zusammen.",
    ]
    assert all(len(caption["words"]) <= 2 for caption in captions)


def test_phrase_fallback_keeps_sentence_punctuation_with_spoken_word():
    captions = phrase_fallback_items("Fertig! Jetzt geht es weiter.", 4, 2)

    assert [caption["text"] for caption in captions] == ["Fertig!", "Jetzt geht", "es weiter."]
    assert all(caption["text"].strip(".!?…") for caption in captions)
