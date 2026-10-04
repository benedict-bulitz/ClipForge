"""Topic Intelligence: discover, judge and propose the next German Knowledge-Short question.

V2 flow (docs/topic-intelligence-v2.md; user-confirmed, never autonomous publishing)::

    DISCOVER   sources: Wikipedia pageviews, YouTube DE chart, Brave News,
               editorial evergreen subjects (evergreen.py)         -> RawTopic
    NORMALIZE  group sightings of one subject (topic != question)  -> TopicGroup
    HARD ELIGIBILITY (cheap)  prefilter persons/tragedy/calendar pages
    DEDUPE     used/skipped topics, novelty vs ClipForge history
    CHEAP EVIDENCE SCORING    curation priority (demand, momentum, outliers)
    SHORTLIST  bounded AI budget, evergreen/live source mix
    QUESTION FORMATION        curator (semantic.py) or seeds/extraction (transform.py)
    SEMANTIC / SHORT-WORTHINESS EVALUATION   gates in scoring.py
    CONFIDENCE-AWARE FINAL RANKING            scoring.score_candidate (the ONE authority)
    SELECT     suggestions (3, diversified) | auto_topic (minimum quality) | next/skip

A confirmed topic is handed to the existing ``POST /api/generation-jobs`` exactly like a
manually typed question (``resolve_topic_provenance``); manual questions never pass
through Topic Intelligence.
"""
