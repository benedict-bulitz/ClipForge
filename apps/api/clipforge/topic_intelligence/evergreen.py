"""Evergreen discovery: the signal class V1 was missing (see docs/topic-intelligence-v2.md).

V1 only discovered what is popular right now (Wikipedia top list, YouTube chart,
news), so a cold-start pool held few knowledge-short subjects and nothing to fall
back on.  This source adds editorial evergreen SUBJECTS with several seed
questions each.  The catalog is only a supply of candidates:

* it never claims demand, momentum or "trending" - those come from the subject's
  real de.wikipedia pageview history (official Wikimedia REST API, bounded, cached);
* every seed question goes through the same eligibility gates, curator judgement
  and scoring as any other candidate - nothing here is a guaranteed winner;
* the catalog lists subjects and questions, never scores.

Bounded: ``SAMPLE_SIZE`` subjects per refresh (rotating by day, the next window on a
widening pass), one pageview request each, cached for a day; after
``MAX_CONSECUTIVE_FAILURES`` failed requests the fetch stops.  Without network the
subjects are still offered - honestly, with no demand evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from .cache import BudgetExceeded, CallMeter, get_or_fetch
from .candidate import RawTopic
from .signals import DEMAND_TTL_HOURS, EVIDENCE_TTL_HOURS, stamp, wikipedia_demand, wikipedia_trend
from .sources import (
    DiscoveryContext,
    HttpGet,
    RateLimited,
    SourceFailed,
    SourceReport,
    SourceResult,
    default_http_get,
)
from .text import topic_key

SOURCE_NAME = "editorial_evergreen"
CATALOG_VERSION = "evergreen-catalog-v1"


@dataclass(frozen=True)
class EvergreenSubject:
    subject: str  # what the video is about (German, display)
    wiki: str  # de.wikipedia article title: the demand/momentum evidence
    niche: str
    questions: tuple[str, ...]  # seed questions; the curator may pick, improve or reject them


def _s(subject: str, wiki: str, niche: str, *questions: str) -> EvergreenSubject:
    return EvergreenSubject(subject, wiki, niche, tuple(questions))


# Editorial criteria for a seed question: one core question, a premise that is
# established knowledge (no myth as premise - myths are asked as "Stimmt es, dass ...?"),
# an answer that fits a short, and something a camera or an animation can show.
CATALOG: tuple[EvergreenSubject, ...] = (
    # Weltraum
    _s("Mars", "Mars (Planet)", "weltraum",
       "Warum ist der Mars rot?",
       "Warum gibt es auf dem Mars so gewaltige Staubstürme?",
       "Warum kann ein Berg auf dem Mars fast dreimal so hoch werden wie der Mount Everest?"),
    _s("Mond", "Mond", "weltraum",
       "Warum sehen wir immer nur dieselbe Seite des Mondes?",
       "Warum wirkt der Mond am Horizont größer als hoch am Himmel?"),
    _s("Sonne", "Sonne", "weltraum",
       "Warum brennt die Sonne, obwohl es im All keinen Sauerstoff gibt?"),
    _s("Schwarzes Loch", "Schwarzes Loch", "weltraum",
       "Warum kann nicht einmal Licht einem Schwarzen Loch entkommen?"),
    _s("Polarlicht", "Polarlicht", "weltraum",
       "Warum leuchten Polarlichter in verschiedenen Farben?",
       "Warum sieht man Polarlichter fast nur in der Nähe der Pole?"),
    _s("Sternschnuppe", "Meteor", "weltraum",
       "Warum verglühen die meisten Sternschnuppen, bevor sie den Boden erreichen?"),
    _s("Venus", "Venus (Planet)", "weltraum",
       "Warum ist die Venus heißer als der Merkur, obwohl sie weiter von der Sonne entfernt ist?"),
    _s("Saturnringe", "Ringe des Saturn", "weltraum",
       "Woraus bestehen die Ringe des Saturn eigentlich?"),
    _s("Schwerelosigkeit", "Internationale Raumstation", "weltraum",
       "Warum schweben Astronauten in der Raumstation, obwohl dort fast die volle Erdanziehung wirkt?"),
    # Natur & Tiere
    _s("Katzen", "Hauskatze", "natur_tiere",
       "Warum landen Katzen fast immer auf den Füßen?",
       "Wie erzeugen Katzen ihr Schnurren?"),
    _s("Hunde", "Haushund", "natur_tiere",
       "Warum hecheln Hunde, statt zu schwitzen?"),
    _s("Glühwürmchen", "Leuchtkäfer", "natur_tiere",
       "Wie erzeugen Glühwürmchen ihr Licht?"),
    _s("Vogelzug", "Vogelzug", "natur_tiere",
       "Wie finden Zugvögel über Tausende Kilometer ihren Weg?"),
    _s("Kraken", "Kraken", "natur_tiere",
       "Warum haben Kraken drei Herzen?"),
    _s("Flamingos", "Flamingos", "natur_tiere",
       "Warum sind Flamingos rosa?"),
    _s("Honigbiene", "Westliche Honigbiene", "natur_tiere",
       "Warum stirbt eine Honigbiene oft, nachdem sie gestochen hat?",
       "Wie zeigen Bienen anderen Bienen den Weg zu einer Blüte?"),
    _s("Giraffen", "Giraffen", "natur_tiere",
       "Warum wird einer Giraffe nicht schwindelig, wenn sie den Kopf hebt?"),
    _s("Pinguine", "Pinguine", "natur_tiere",
       "Warum frieren Pinguine mit ihren Füßen nicht am Eis fest?"),
    _s("Herbstlaub", "Herbstfärbung", "natur_tiere",
       "Warum werden Blätter im Herbst gelb und rot?"),
    _s("Hotspot-Vulkane", "Hotspot (Geologie)", "natur_tiere",
       "Warum gibt es auf Hawaii Vulkane, obwohl es mitten auf einer Erdplatte liegt?"),
    _s("Erdbeben", "Erdbeben", "natur_tiere",
       "Warum kann man Erdbeben bis heute nicht vorhersagen?"),
    _s("Ameisen", "Ameisen", "natur_tiere",
       "Wie können Ameisen ein Vielfaches ihres eigenen Gewichts tragen?"),
    # Wetter & Klima
    _s("Blitz", "Blitz", "wetter_klima",
       "Warum schlägt ein Blitz im Zickzack ein?"),
    _s("Regenbogen", "Regenbogen", "wetter_klima",
       "Warum ist ein Regenbogen eigentlich ein Kreis?"),
    _s("Himmelsfarbe", "Rayleigh-Streuung", "wetter_klima",
       "Warum ist der Himmel blau und nicht violett?",
       "Warum färbt sich der Himmel bei Sonnenuntergang rot?"),
    _s("Hagel", "Hagel", "wetter_klima",
       "Wie können Hagelkörner so groß wie Tennisbälle werden?"),
    _s("Schneeflocken", "Schneekristall", "wetter_klima",
       "Warum hat jede Schneeflocke sechs Arme?"),
    _s("Wolken", "Wolke", "wetter_klima",
       "Warum fallen Wolken nicht vom Himmel, obwohl sie Tonnen von Wasser enthalten?"),
    # Körper & Gesundheit
    _s("Muskelkater", "Muskelkater", "koerper_gesundheit",
       "Warum kommt Muskelkater erst einen Tag nach dem Sport?"),
    _s("Gänsehaut", "Gänsehaut", "koerper_gesundheit",
       "Warum bekommen wir Gänsehaut, wenn uns kalt ist?"),
    _s("Kitzeln", "Kitzeln", "koerper_gesundheit",
       "Warum kann man sich nicht selbst kitzeln?"),
    _s("Kältekopfschmerz", "Kältekopfschmerz", "koerper_gesundheit",
       "Warum bekommt man von zu schnell gegessenem Eis Kopfschmerzen?"),
    _s("Schrumpelfinger", "Haut", "koerper_gesundheit",
       "Warum werden Finger im Wasser schrumpelig?"),
    _s("Herzschlag", "Sinusknoten", "koerper_gesundheit",
       "Warum schlägt das Herz auch ohne Befehl vom Gehirn?"),
    _s("Placebo", "Placebo", "koerper_gesundheit",
       "Wie kann ein Placebo wirken, obwohl kein Wirkstoff darin ist?"),
    _s("Koffein", "Koffein", "koerper_gesundheit",
       "Warum macht Kaffee wach?"),
    _s("Schlafträgheit", "Schlafträgheit", "koerper_gesundheit",
       "Warum fühlt man sich nach einem langen Mittagsschlaf oft müder als vorher?"),
    # Psychologie
    _s("Vergessen", "Vergessen", "psychologie",
       "Warum vergessen wir oft, was wir wollten, sobald wir einen Raum betreten?"),
    # Wissenschaft & Alltag
    _s("Eis", "Dichteanomalie", "wissenschaft",
       "Warum schwimmt Eis auf Wasser?"),
    _s("Mikrowelle", "Mikrowellenherd", "technik",
       "Wie erhitzt eine Mikrowelle eigentlich das Essen?"),
    _s("Seifenblasen", "Seifenblase", "wissenschaft",
       "Warum schillern Seifenblasen in allen Farben?"),
    _s("Streusalz", "Streusalz", "alltag_phaenomene",
       "Warum taut Salz das Eis auf der Straße?"),
    _s("Spiegel", "Spiegel", "wissenschaft",
       "Warum vertauscht ein Spiegel links und rechts, aber nicht oben und unten?"),
    _s("Fliegen", "Dynamischer Auftrieb", "technik",
       "Wie kann ein tonnenschweres Flugzeug überhaupt fliegen?"),
    _s("Schiffe", "Archimedisches Prinzip", "wissenschaft",
       "Warum schwimmt ein Schiff aus Stahl?"),
    _s("Golfball", "Golfball", "wissenschaft",
       "Warum hat ein Golfball Dellen?"),
    _s("Glas", "Glas", "wissenschaft",
       "Warum ist Glas durchsichtig?"),
    _s("Handyakku", "Lithium-Ionen-Akkumulator", "technik",
       "Warum verlieren Handyakkus mit der Zeit an Kapazität?"),
    _s("GPS", "Global Positioning System", "technik",
       "Woher weiß das Handy per Satellit, wo man gerade ist?"),
    _s("Touchscreen", "Touchscreen", "technik",
       "Wie merkt ein Touchscreen, wo der Finger ist?"),
    _s("Glasfaser", "Lichtwellenleiter", "technik",
       "Wie kann Licht in einem Glasfaserkabel um die Kurve laufen?"),
    _s("Kühlschrank", "Kühlschrank", "technik",
       "Wie kann ein Kühlschrank kühlen, obwohl er hinten warm wird?"),
    _s("QR-Code", "QR-Code", "technik",
       "Warum funktioniert ein QR-Code auch dann noch, wenn er zerkratzt ist?"),
    _s("Geräuschunterdrückung", "Active Noise Cancelling", "technik",
       "Wie kann ein Kopfhörer Lärm mit Schall auslöschen?"),
    # Essen & Trinken
    _s("Popcorn", "Popcorn", "essen_trinken",
       "Warum platzt Mais beim Erhitzen zu Popcorn auf?"),
    _s("Altbackenes Brot", "Brot", "essen_trinken",
       "Warum wird Brot an der Luft hart, Kekse aber weich?"),
    _s("Chili", "Capsaicin", "essen_trinken",
       "Warum brennt Chili im Mund, obwohl es gar nicht heiß ist?"),
    _s("Zwiebeln", "Zwiebel", "essen_trinken",
       "Warum muss man beim Zwiebelschneiden weinen?"),
    _s("Kohlensäure", "Kohlensäure", "essen_trinken",
       "Warum schäumt eine geschüttelte Flasche Cola über?"),
    _s("Honig", "Honig", "essen_trinken",
       "Warum wird Honig praktisch nie schlecht?"),
    # Geschichte
    _s("Wikinger", "Wikinger", "geschichte",
       "Stimmt es, dass Wikinger Helme mit Hörnern trugen?"),
    _s("Flache Erde im Mittelalter", "Flache Erde", "geschichte",
       "Stimmt es, dass die Menschen im Mittelalter die Erde für eine Scheibe hielten?"),
    _s("Römischer Beton", "Opus caementicium", "geschichte",
       "Warum hält römischer Beton seit 2000 Jahren?"),
    _s("Zeitzonen", "Zeitzone", "geschichte",
       "Warum gibt es überhaupt Zeitzonen?"),
    # Geografie
    _s("Totes Meer", "Totes Meer", "geografie",
       "Warum geht man im Toten Meer nicht unter?"),
    _s("Salziges Meer", "Meerwasser", "geografie",
       "Warum ist das Meer salzig, Flüsse aber nicht?"),
    _s("Mount Everest", "Mount Everest", "geografie",
       "Warum wird der Mount Everest jedes Jahr ein Stück höher?"),
    _s("Grüne Sahara", "Grüne Sahara", "geografie",
       "Warum war die Sahara früher grün?"),
    _s("Bermudadreieck", "Bermudadreieck", "geografie",
       "Stimmt es, dass im Bermudadreieck mehr Schiffe verschwinden als anderswo?"),
)

SAMPLE_SIZE = 24  # subjects (= pageview requests) per refresh
HISTORY_DAYS = 60
MAX_CONSECUTIVE_FAILURES = 3


def sample_window(day: datetime, *, widen: bool = False, size: int = SAMPLE_SIZE) -> tuple[int, list[EvergreenSubject]]:
    """(offset, subjects): a rotating daily window; a widening pass takes the next one."""
    count = len(CATALOG)
    size = min(size, count)
    offset = (day.toordinal() * size + (size if widen else 0)) % count
    return offset, [CATALOG[(offset + index) % count] for index in range(size)]


class EvergreenCatalogSource:
    name = SOURCE_NAME

    def __init__(self, http_get: HttpGet = default_http_get) -> None:
        self.http_get = http_get

    def fetch(self, subjects: list[EvergreenSubject], day: datetime, meter: CallMeter) -> dict[str, Any]:
        start = (day - timedelta(days=HISTORY_DAYS - 1)).strftime("%Y%m%d")
        end = day.strftime("%Y%m%d")
        histories: dict[str, list[int]] = {}
        errors: list[str] = []
        failures = 0
        rate_limited = False
        for subject in subjects:
            if failures >= MAX_CONSECUTIVE_FAILURES:
                errors.append(f"stopped after {failures} consecutive failures")
                break
            meter.charge()
            try:
                response = self.http_get(
                    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/de.wikipedia/all-access/user/"
                    f"{quote(subject.wiki.replace(' ', '_'), safe='')}/daily/{start}/{end}",
                    {},
                    {},
                )
            except RateLimited:
                # HTTP 429: stop at once (no retry storm); what was measured is kept, briefly cached.
                rate_limited = True
                errors.append(f"rate limited (HTTP 429) after {len(histories)} subjects; remaining subjects without evidence")
                break
            except FileNotFoundError:
                failures = 0  # the article exists under another title: no evidence for this subject only
                errors.append(f"no article: {subject.wiki}")
                continue
            except (SourceFailed, BudgetExceeded) as exc:
                failures += 1
                errors.append(f"{subject.wiki}: {exc}")
                continue
            failures = 0
            histories[subject.wiki] = [int(item.get("views") or 0) for item in response.get("items") or []]
        if not histories:
            # Nothing measured: do not cache "no evidence" for a day; the subjects are offered without it.
            raise SourceFailed("; ".join(errors[:3]) or "no pageview history")
        return {"day": day.strftime("%Y-%m-%d"), "histories": histories, "partial_errors": errors[:10],
                **({"rate_limited": True} if rate_limited else {})}

    def topics(self, subjects: list[EvergreenSubject], payload: dict[str, Any], observed_at: datetime) -> list[RawTopic]:
        histories = payload.get("histories") or {}
        topics = []
        for subject in subjects:
            history = histories.get(subject.wiki) or []
            momentum = stamp(wikipedia_trend(history), observed_at, ttl_hours=EVIDENCE_TTL_HOURS["wikipedia_evergreen"]) if history else None
            demand = stamp(wikipedia_demand(history), observed_at, ttl_hours=DEMAND_TTL_HOURS) if history else None
            topics.append(RawTopic(
                key=topic_key(subject.subject),
                title=subject.subject,
                source=self.name,
                kind="evergreen",
                observed_at=observed_at,
                description=f"Zeitloses Wissensthema ({subject.niche}); Wikipedia: {subject.wiki}",
                url=f"https://de.wikipedia.org/wiki/{quote(subject.wiki.replace(' ', '_'))}",
                trend=momentum,
                demand=demand,
                metrics={
                    "catalog": CATALOG_VERSION,
                    "wiki": subject.wiki,
                    "niche": subject.niche,
                    **({"median_views_per_day": demand.evidence.get("median_views_per_day")} if demand is not None and demand.available else {}),
                    **({"ratio": momentum.evidence.get("ratio")} if momentum is not None and momentum.available else {}),
                },
                seed_questions=subject.questions,
            ))
        return topics

    def discover(self, ctx: DiscoveryContext) -> SourceResult:
        day = ctx.now - timedelta(days=1)
        offset, subjects = sample_window(day, widen=getattr(ctx, "widen", False))
        calls_before = ctx.meter.calls
        try:
            hit = get_or_fetch(
                ctx.db, self.name, f"pageviews:{CATALOG_VERSION}:{day:%Y-%m-%d}:{offset}",
                lambda meter: self.fetch(subjects, day, meter), ctx.meter, now=ctx.now,
            )
        except Exception as exc:  # noqa: BLE001 - evidence missing, never the subjects
            ctx.db.rollback()
            topics = self.topics(subjects, {}, ctx.now)
            return SourceResult(topics, SourceReport(
                self.name, "partial", error=f"pageview evidence unavailable: {type(exc).__name__}: {str(exc)[:140]}",
                fetched_at=ctx.now, calls=ctx.meter.calls - calls_before, items=len(topics),
                detail={"catalog": CATALOG_VERSION, "offset": offset, "evidence": False},
            ))
        topics = self.topics(subjects, hit.payload, hit.fetched_at)
        partial = hit.payload.get("partial_errors") or []
        return SourceResult(topics, SourceReport(
            self.name, "partial" if partial else "cached" if hit.cached else "ok", error="; ".join(partial[:3]) or None,
            fetched_at=hit.fetched_at, calls=hit.calls, items=len(topics),
            detail={"catalog": CATALOG_VERSION, "offset": offset, "evidence": True, "measured": len(hit.payload.get("histories") or {})},
        ))
