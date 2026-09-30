"""Topic Intelligence V1: discover, score and propose the next German video topic.

Flow (user-confirmed, never autonomous publishing)::

    sources (Wikipedia pageviews, YouTube DE, Brave News)  -> RawTopic
    transform (topic -> natural German question)           -> TopicCandidate
    signals (trend, outlier, competition, novelty, fit, own performance)
    scoring.score_candidate  (the ONE scoring authority)   -> ranked pool
    service.next_topic / skip_topic                        -> proposal
    service.resolve_topic_provenance                       -> existing
        POST /api/generation-jobs -> generation.create_generation_job

This package never creates projects or runs the video pipeline itself: a
confirmed topic is handed to the existing generation entry point exactly like
a manually typed question.
"""
