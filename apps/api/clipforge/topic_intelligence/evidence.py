"""Optional candidate evidence: the injection point for the future Analytics Learning Loop.

Topic Intelligence V2 must work in cold start, so own-channel performance is NOT a
primary signal today (``history.own_performance_priors`` stays a small niche prior that
is "unavailable" until enough uploads exist).  Later evidence - Stayed to watch,
retention, average percentage viewed, engagement, topic-family or question-mechanism
performance - plugs in here without touching discovery or the scoring authority:

    class RetentionByFamily:                       # implements CandidateEvidenceProvider
        name = "retention_by_family"
        def signals(self, candidate):
            return {"own_performance": Signal(0.7, "medium", {...}, ["own_analytics"])}

    deps = DiscoveryDeps(..., evidence_providers=[RetentionByFamily(...)])

Rules (enforced in ``apply_evidence``):

* a provider may only fill signal names in ``ANALYTICS_SIGNALS`` - it can never create
  demand, momentum, outlier or trending status, and never overwrite question quality;
* a provider that fails or returns garbage is skipped (recorded in provenance); the
  candidate keeps its cold-start signals;
* the weight of these signals lives in the scoring authority (overridable via
  TOPIC_SCORE_WEIGHTS) and is confidence-scaled like every other signal, so thin
  analytics cannot dominate - and their absence never lowers a candidate's confidence.
"""

from typing import Protocol

from .candidate import Signal, TopicCandidate

# Signal names analytics evidence may fill.  Add new names here AND a weight in the
# scoring authority (and its OPTIONAL_SIGNALS) when the learning loop introduces them.
ANALYTICS_SIGNALS = frozenset({"own_performance"})


class CandidateEvidenceProvider(Protocol):
    name: str

    def signals(self, candidate: TopicCandidate) -> dict[str, Signal]: ...


def apply_evidence(candidate: TopicCandidate, providers: list[CandidateEvidenceProvider]) -> list[str]:
    """Merge provider signals into the candidate (allow-listed names only); returns applied names."""
    applied: list[str] = []
    for provider in providers:
        name = str(getattr(provider, "name", type(provider).__name__))
        try:
            produced = provider.signals(candidate) or {}
        except Exception as exc:  # noqa: BLE001 - optional evidence never breaks Topic Intelligence
            candidate.provenance.setdefault("evidence_errors", []).append(f"{name}: {type(exc).__name__}")
            continue
        for signal_name, signal in produced.items():
            if signal_name not in ANALYTICS_SIGNALS or not isinstance(signal, Signal):
                candidate.provenance.setdefault("evidence_errors", []).append(f"{name}: {signal_name} not allowed")
                continue
            current = candidate.signals.get(signal_name)
            # Never replace measured evidence by weaker evidence.
            order = ("unavailable", "low", "medium", "high")
            if current is not None and current.available and order.index(current.confidence) > order.index(signal.confidence):
                continue
            candidate.signals[signal_name] = signal
            applied.append(f"{name}:{signal_name}")
    if applied:
        candidate.provenance["evidence_providers"] = applied
    return applied
