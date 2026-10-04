"""Research Pipeline V2 - offline evaluation harness.

A deterministic fake web (``research_v2_support.FakeWeb``) stands in for
search providers and sites.  Fixtures: strong primary source, generic SEO
article, syndicated copies, stale current article, conflicting sources,
JS-only page, extraction failure, causal explanation, surface fact without
mechanism, insufficient evidence, numerical claim with and without evidence.
Topics are fixtures only; production code has no topic vocabulary.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from clipforge.research_v2 import extraction, run_research
from clipforge.research_v2.evidence import units_from_paragraphs
from clipforge.research_v2.models import ResearchBudget, SubQuestion
from clipforge.research_v2.package import validate_synthesized
from clipforge.research_v2.quality import classify_source, registrable_domain
from clipforge.research_v2.retrieval import Retriever
from clipforge.research_v2.routing import route_question
from clipforge.research_v2.synthesis import DecompositionOut, SynthesisOut
from research_v2_support import FakeWeb, brave_hit, html_page, research_settings

MICRO = "Warum bleibt Essen in der Mikrowelle in der Mitte kalt?"
UNI = "https://www.uni-beispiel.de/physik/mikrowelle"
GOV = "https://www.lebensmittel.bund.de/mikrowelle"
SEO = "https://kuechen-tipps-blog.com/top-10-mikrowelle"
JUPITER = "https://science.raumfahrt.gov/jupiter"

UNI_PAGE = html_page("Wie die Mikrowelle Essen erwärmt", [
    "Mikrowellen dringen nur etwa 1 bis 2,5 Zentimeter tief in das Essen ein, deshalb wird zuerst der äußere Rand erwärmt.",
    "Die Mitte des Essens wird nur durch Wärmeleitung von außen erwärmt, und das dauert in der Mikrowelle deutlich länger.",
    "Viele glauben, dass Mikrowellen das Essen von innen nach außen erhitzen, doch das ist ein Irrtum.",
], published="2021-03-01", site="Universität Beispiel")
GOV_PAGE = html_page("Mikrowelle richtig nutzen", [
    "Essen erwärmt sich in der Mikrowelle ungleichmäßig, weil die Mikrowellen nur die äußeren Zentimeter des Essens direkt erreichen.",
    "Gefrorene Bereiche in der Mitte nehmen die Energie der Mikrowelle schlechter auf als aufgetaute Bereiche am Rand.",
], published="2022-01-10", site="Bundesamt Beispiel")
SEO_PAGE = html_page("Top 10 Mikrowellen-Tricks, die du kennen musst", [
    "Essen in der Mikrowelle bleibt in der Mitte kalt, weil die Mikrowelle einfach nicht stark genug ist, sagen viele Leute.",
    "Mit unserer Testsieger-Mikrowelle (Affiliate-Link, Werbung) bleibt Essen in der Mitte nie mehr kalt, jetzt zugreifen.",
])


def _micro_web() -> FakeWeb:
    web = FakeWeb()
    web.brave = {"Mikrowelle": [
        brave_hit(SEO, "Top 10 Mikrowellen-Tricks", "Essen in der Mikrowelle bleibt in der Mitte kalt? Diese Tricks helfen."),
        brave_hit(UNI, "Wie die Mikrowelle Essen erwärmt", "Mikrowellen dringen nur wenige Zentimeter in Essen ein."),
        brave_hit(GOV, "Mikrowelle richtig nutzen", "Essen erwärmt sich in der Mikrowelle ungleichmäßig."),
    ]}
    web.page(UNI, UNI_PAGE)
    web.page(GOV, GOV_PAGE)
    web.page(SEO, SEO_PAGE)
    return web


def _run(web: FakeWeb, question: str = MICRO, *, llm=None, today: date | None = None, **settings):
    values = {"brave_search_api_key": "test-key", **settings}
    return run_research(question, "de", research_settings(**values), context={"question": question},
                        transport=web.transport(), llm=llm, cache_root=None, today=today)


def _role(run, role: str) -> list[dict]:
    return [fact for fact in run.facts if fact["research_role"] == role]


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------

def test_authoritative_relevant_sources_win_over_seo():
    run = _run(_micro_web())
    core = _role(run, "core_answer")[0]
    assert {source["source_type"] for source in core["sources"]} <= {"academic", "government"}
    assert "Zentimeter" in core["claim"] and core["verification"] == "supported"
    # The SEO page was retrieved, classified and never used.
    seo = next(source for source in run.diagnostics["sources"] if source["url"] == SEO)
    assert seo["source_type"] == "low_quality" and not seo["used"]
    assert all("Affiliate" not in fact["claim"] and "nicht stark genug" not in fact["claim"] for fact in run.facts)
    assert run.package["status"] == "sufficient"


def test_irrelevant_authority_does_not_win():
    web = _micro_web()
    web.brave["Mikrowelle"].insert(0, brave_hit(JUPITER, "Jupiter", "Mikrowelle Mitte Essen kalt Raumfahrt Jupiter"))
    web.page(JUPITER, html_page("Jupiter und seine Monde", [
        "Jupiter ist der größte Planet des Sonnensystems und besteht überwiegend aus Wasserstoff und Helium.",
        "Seine Monde wurden 1610 entdeckt und werden seitdem von Raumsonden genau untersucht.",
    ], site="Raumfahrtbehörde"))
    run = _run(web)
    jupiter = next(source for source in run.diagnostics["sources"] if source["url"] == JUPITER)
    assert jupiter["source_type"] == "government" and jupiter["evidence_units"] == 0 and not jupiter["used"]
    assert all("Jupiter" not in fact["claim"] for fact in run.facts)


def test_syndicated_copies_do_not_inflate_confidence():
    web = FakeWeb()
    text = [
        "Der Himmel auf dem Mars ist rot, weil feiner Staub in der Atmosphäre das blaue Licht absorbiert und rotes Licht streut.",
        "Die Staubteilchen sind etwa 1,5 Mikrometer groß und werden von Winden ständig in der Mars-Atmosphäre gehalten.",
    ]
    copies = [f"https://www.nachrichten-{name}.de/wissen/mars-himmel" for name in ("nord", "sued", "west")]
    web.brave = {"Mars": [brave_hit(url, "Warum der Mars-Himmel rot ist", "Mars Himmel rot Staub") for url in copies]}
    for url in copies:
        web.page(url, html_page("Warum der Mars-Himmel rot ist", ["(dpa) " + text[0], text[1]], published="2024-02-01", site=url))
    run = _run(web, "Warum ist der Himmel auf dem Mars rot?")
    core = _role(run, "core_answer")[0]
    assert core["independent_sources"] == 1 and len(core["sources"]) == 1
    assert core["verification"] == "source_attributed" and core["confidence"] < 0.85
    notes = {source["independence_note"] for source in run.diagnostics["sources"]}
    assert notes & {"near-duplicate text (syndicated copy)", "same upstream (dpa)"}


def test_independent_agreement_raises_confidence():
    run = _run(_micro_web())
    core = _role(run, "core_answer")[0]
    assert core["independent_sources"] == 2 and core["confidence"] == 0.9


def test_stale_current_evidence_is_rejected():
    web = FakeWeb()
    old, new = "https://www.zeitung-a.de/streik-2019", "https://www.zeitung-b.de/streik-heute"
    web.brave = {"Lokführer": [brave_hit(old, "Streik der Lokführer", "Lokführer streiken"), brave_hit(new, "Streik der Lokführer", "Lokführer streiken")]}
    web.page(old, html_page("Streik der Lokführer", [
        "Die Lokführer streiken derzeit, weil die Gewerkschaft eine Lohnerhöhung von zehn Prozent fordert.",
    ], published="2019-05-01", schema="NewsArticle"))
    web.page(new, html_page("Streik der Lokführer", [
        "Die Lokführer streiken aktuell, weil die Gewerkschaft kürzere Arbeitszeiten für Schichtarbeiter fordert.",
    ], published="2026-09-28", schema="NewsArticle"))
    run = _run(web, "Warum streiken die Lokführer heute?", today=date(2026, 10, 4))
    assert run.package["route"]["domain"] == "current_events"
    assert all("zehn Prozent" not in fact["claim"] for fact in run.facts)
    assert any("Arbeitszeiten" in fact["claim"] for fact in run.facts)
    assert any(item.get("reason") == "stale_for_time_sensitive_claim" for item in run.package["rejected_claims"])


def test_evergreen_facts_are_not_penalised_for_old_pages():
    web = _micro_web()
    web.page(UNI, UNI_PAGE.replace("2021-03-01", "2003-01-01"))
    run = _run(web, today=date(2026, 10, 4))
    assert "Zentimeter" in _role(run, "core_answer")[0]["claim"]


def test_conflicting_numbers_are_recorded_not_silently_chosen():
    web = FakeWeb()
    a, b = "https://www.uni-alpha.de/mond", "https://www.institut-beta.org/mond"
    web.brave = {"Mond": [brave_hit(a, "Mond Entfernung", "Mond Erde Entfernung"), brave_hit(b, "Mond Entfernung", "Mond Erde Entfernung")]}
    web.page(a, html_page("Mond", ["Der Mond entfernt sich jedes Jahr um etwa 3,8 Zentimeter von der Erde, weil Gezeiten Energie übertragen."]))
    web.page(b, html_page("Mond", ["Der Mond entfernt sich jedes Jahr um etwa 9,5 Zentimeter von der Erde, weil Gezeiten Energie übertragen."]))
    run = _run(web, "Warum entfernt sich der Mond jedes Jahr von der Erde?")
    assert run.package["contradictions"] and run.package["contradictions"][0]["type"] == "numeric"
    assert run.package["contradictions"][0]["resolution"].startswith("unresolved")
    assert all("3,8" not in fact["claim"] and "9,5" not in fact["claim"] for fact in run.facts)
    assert "unresolved_contradiction" in run.package["gaps"]


def test_mechanism_evidence_is_required_for_why_questions():
    web = FakeWeb()
    url = "https://www.uni-gamma.de/mikrowelle"
    web.brave = {"Mikrowelle": [brave_hit(url, "Mikrowelle", "Essen Mikrowelle Mitte kalt")]}
    web.page(url, html_page("Mikrowelle", [
        "Essen in der Mikrowelle ist in der Mitte oft noch kalt, während der Rand schon dampft.",
        "Die erste Mikrowelle für Haushalte kam im Jahr 1967 auf den Markt und war sehr teuer.",
    ]))
    run = _run(web)
    assert run.package["status"] == "missing_mechanism"
    assert "no_mechanism_evidence" in run.package["gaps"]
    assert run.package["explanation_spine"]["status"] == "missing_mechanism"
    assert run.facts  # the surface fact is kept; the existing bounded retry looks for the mechanism


def test_extraction_failure_and_dead_sites_are_isolated():
    web = _micro_web()
    broken, js_only, down = "https://www.physik-forum-beispiel.de/x", "https://app.mikrowellen-rechner.de/", "https://down.example-wissen.de/a"
    web.brave["Mikrowelle"] += [
        brave_hit(broken, "Mikrowelle Mitte", "Essen Mikrowelle Mitte kalt"),
        brave_hit(js_only, "Mikrowelle Rechner", "Essen Mikrowelle Mitte kalt"),
        brave_hit(down, "Mikrowelle Wissen", "Essen Mikrowelle Mitte kalt"),
    ]
    web.page(broken, "<html><body><div><span>kaputt</div></p></body", status=200)
    web.page(js_only, '<html><body><div id="root"></div><noscript>You need to enable JavaScript to run this app.</noscript>'
             + "<script></script>" * 6 + "</body></html>")
    web.fail_hosts.add("down.example-wissen.de")
    run = _run(web, research_max_documents=8)
    statuses = {item["url"]: item["status"] for item in run.diagnostics["retrieval"]}
    assert statuses[broken] == "extraction_failed"
    assert statuses[js_only] == "js_only"
    assert statuses[down] == "network_error"
    assert run.status == "verified_sources" and "Zentimeter" in _role(run, "core_answer")[0]["claim"]


def test_js_only_page_uses_bounded_dynamic_render_only_when_enabled():
    web = FakeWeb()
    url = "https://app.planetarium-beispiel.de/mars"
    web.brave = {"Mars": [brave_hit(url, "Mars", "Himmel Mars rot")]}
    web.page(url, '<html><body><div id="app"></div><noscript>Please enable JavaScript.</noscript></body></html>')
    rendered = html_page("Mars", ["Der Himmel auf dem Mars ist rot, weil feiner Staub in der Atmosphäre das blaue Licht absorbiert."])
    calls: list[str] = []

    def dynamic(target: str, _timeout: float) -> str:
        calls.append(target)
        return rendered

    settings = research_settings(brave_search_api_key="k")
    off = run_research("Warum ist der Himmel auf dem Mars rot?", "de", settings, context={}, transport=web.transport(),
                       llm=None, dynamic_fetch=dynamic, cache_root=None)
    assert not calls and not off.facts
    on_settings = research_settings(brave_search_api_key="k", research_browser_fetch=True)
    on = run_research("Warum ist der Himmel auf dem Mars rot?", "de", on_settings, context={}, transport=web.transport(),
                      llm=None, dynamic_fetch=dynamic, cache_root=None)
    assert calls == [url] and on.facts and on.diagnostics["budget"]["used"]["dynamic"] == 1
    assert on.diagnostics["retrieval"][0]["method"] == "dynamic"


def test_insufficient_core_evidence_yields_no_facts():
    web = FakeWeb()
    url = "https://www.uni-delta.de/kueche"
    web.brave = {"Mikrowelle": [brave_hit(url, "Küche", "Mikrowelle")]}
    web.page(url, html_page("Küchengeräte", [
        "Viele Küchengeräte wurden im zwanzigsten Jahrhundert für den Haushalt entwickelt und verbreitet.",
    ]))
    run = _run(web)
    assert run.package["status"] == "insufficient" and run.facts == [] and run.status == "unavailable"
    assert "no_direct_answer" in run.package["gaps"]


def test_numerical_claim_with_evidence_is_traceable():
    run = _run(_micro_web())
    core = _role(run, "core_answer")[0]
    assert "2,5" in core["claim"]
    evidence = {item["id"]: item for item in run.package["evidence"]}
    assert all(evidence_id in evidence for evidence_id in core["evidence_ids"])
    assert any("2.5" in evidence[evidence_id]["numbers"] for evidence_id in core["evidence_ids"])
    ref = run.package["core_answer"]
    assert ref["evidence_ids"] == core["evidence_ids"] and ref["source_ids"]


class FakeLLM:
    def __init__(self, synthesis: dict, decomposition: dict | None = None):
        self.synthesis = synthesis
        self.decomposition = decomposition
        self.calls = 0

    def decompose(self, question, language):
        self.calls += 1
        return DecompositionOut(**self.decomposition) if self.decomposition else None

    def synthesize(self, payload):
        self.calls += 1
        self.payload = payload
        return SynthesisOut(**self.synthesis)


def _evidence_id(run_payload: dict, needle: str) -> str:
    return next(item["id"] for item in run_payload["evidence"] if needle in item["text"])


def test_unsupported_claims_cannot_enter_the_package():
    web = _micro_web()
    probe = _run(web)
    depth = next(item for item in probe.package["evidence"] if "Zentimeter" in item["text"])["id"]
    conduction = next(item for item in probe.package["evidence"] if "Wärmeleitung" in item["text"])["id"]
    llm = FakeLLM({
        "core_answer": {"text": "Mikrowellen erwärmen nur die äußeren 1 bis 2,5 Zentimeter des Essens direkt.", "evidence_ids": [depth]},
        "mechanism_steps": [
            {"text": "Die Mitte wird nur durch Wärmeleitung von außen warm, und das dauert länger.", "evidence_ids": [conduction]},
            # Invented number: not in the cited evidence.
            {"text": "Die Mitte erreicht nach 90 Sekunden nur 12 Grad.", "evidence_ids": [conduction]},
            # Invented cause with no evidence at all.
            {"text": "Der Drehteller dreht sich zu langsam, deshalb bleibt die Mitte kalt.", "evidence_ids": []},
            # Unknown evidence ID.
            {"text": "Wasser bindet Strahlung besonders stark.", "evidence_ids": ["ev_99"]},
        ],
        "viewer_takeaway": {"text": "Die Mikrowelle erwärmt zuerst den äußeren Rand, die Mitte wird nur durch Wärmeleitung erwärmt.", "evidence_ids": [depth, conduction]},
    })
    run = _run(web, llm=llm)
    assert run.package["synthesis"] == "llm_validated" and llm.calls == 2
    claims = " ".join(fact["claim"] for fact in run.facts)
    assert "90 Sekunden" not in claims and "Drehteller" not in claims and "Strahlung besonders" not in claims
    reasons = {item["reason"].split(":")[0] for item in run.package["rejected_claims"]}
    assert {"number_not_in_evidence", "no_evidence_cited", "unknown_evidence_ids"} <= reasons
    assert run.package["explanation_spine"]["viewer_takeaway"].startswith("Die Mikrowelle erwärmt zuerst")
    # The model only ever saw compact evidence units, never page text.
    assert all(set(item) >= {"id", "text", "source_id"} and len(item["text"]) <= 400 for item in llm.payload["evidence"])


def test_causal_claim_needs_causal_evidence():
    unit = units_from_paragraphs(
        ["Essen in der Mikrowelle ist in der Mitte oft noch kalt, während der Rand schon heiß ist."],
        source_id="src_01", sub_questions=[SubQuestion("q_core", "core", MICRO, "Essen Mikrowelle Mitte kalt")],
        core_terms={"essen", "mikrowelle", "mitte", "kalt"}, start=1,
    )[0]
    reason = validate_synthesized("Die Mitte bleibt kalt, weil der Drehteller sich zu langsam dreht.", [unit.id], {unit.id: unit})
    assert reason in {"content_not_in_evidence", "causal_claim_without_causal_evidence"}


def test_llm_synthesis_without_core_falls_back_to_verbatim_evidence():
    llm = FakeLLM({"mechanism_steps": [{"text": "Erfunden ohne Beleg.", "evidence_ids": ["ev_77"]}]})
    run = _run(_micro_web(), llm=llm)
    assert run.package["synthesis"] == "llm_rejected_fallback_deterministic"
    assert "Zentimeter" in _role(run, "core_answer")[0]["claim"]


def test_decomposition_is_bounded_and_core_first():
    llm = FakeLLM({}, decomposition={"domain": "science", "sub_questions": [
        {"kind": "mechanism", "question": "Wie erwärmen Mikrowellen Essen?", "query": "Mikrowelle Erwärmung Eindringtiefe",
         "english_query": "microwave heating penetration depth"},
        {"kind": "core", "question": MICRO, "query": "Mikrowelle Mitte kalt"},
        {"kind": "misconception", "question": "Erhitzt die Mikrowelle von innen?", "query": "Mikrowelle von innen Irrtum"},
    ]})
    run = _run(_micro_web(), llm=llm)
    subs = run.diagnostics["sub_questions"]
    assert subs[0]["kind"] == "core" and len(subs) <= 4 and run.diagnostics["decomposition"] == "llm"
    assert run.diagnostics["budget"]["used"]["llm_calls"] <= 2
    assert run.diagnostics["budget"]["used"]["searches"] <= run.diagnostics["budget"]["limits"]["searches"]


# ---------------------------------------------------------------------------
# Retrieval, extraction, classification units
# ---------------------------------------------------------------------------

def test_extraction_drops_boilerplate_and_keeps_metadata():
    page = extraction.extract_page(UNI_PAGE, UNI)
    text = " ".join(page.paragraphs)
    assert "Cookies" not in text and "Mehr zum Thema" not in text and "Alle Rechte" not in text
    assert page.published_at == "2021-03-01" and page.site_name == "Universität Beispiel" and len(page.paragraphs) == 3


def test_extraction_works_without_scrapling(monkeypatch):
    monkeypatch.setattr(extraction, "_ScraplingSelector", None)
    page = extraction.extract_page(UNI_PAGE, UNI)
    assert page.extractor == "builtin" and len(page.paragraphs) == 3 and "Cookies" not in " ".join(page.paragraphs)


def test_robots_access_control_and_rate_limits_are_respected():
    web = FakeWeb()
    web.robots["blocked.example.org"] = "User-agent: *\nDisallow: /private\n"
    web.page("https://blocked.example.org/private/a", UNI_PAGE)
    web.page("https://paywall.example.org/a", "Login required", status=402)
    web.page("https://busy.example.org/a", "slow down", status=429)
    web.page("https://busy.example.org/b", UNI_PAGE)
    retriever = Retriever(ResearchBudget(max_documents=10), transport=web.transport())
    try:
        results = {o.url: o.status for o in retriever.fetch_many([
            "https://blocked.example.org/private/a", "https://paywall.example.org/a", "https://busy.example.org/a",
        ])}
        later = retriever.fetch("https://busy.example.org/b")
    finally:
        retriever.close()
    assert results == {
        "https://blocked.example.org/private/a": "robots_disallowed",
        "https://paywall.example.org/a": "access_denied",
        "https://busy.example.org/a": "rate_limited",
    }
    assert later.status == "host_skipped"
    assert "https://blocked.example.org/private/a" not in web.requests


def test_retries_and_documents_are_budgeted():
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        attempts.append(str(request.url))
        return httpx.Response(503)

    budget = ResearchBudget(max_documents=2, max_retries=1)
    retriever = Retriever(budget, transport=httpx.MockTransport(handler))
    try:
        outcomes = [retriever.fetch(f"https://a{index}.example.org/x") for index in range(3)]
    finally:
        retriever.close()
    assert [o.status for o in outcomes] == ["http_error", "http_error", "budget_exhausted"]
    assert len(attempts) == 3  # 2 documents + exactly one retry in total


def test_cache_serves_extracted_pages_not_raw_html(tmp_path):
    web = _micro_web()
    first = _run(web)
    requests = len(web.requests)
    settings = research_settings(brave_search_api_key="test-key")
    run_research(MICRO, "de", settings, context={"question": MICRO}, transport=web.transport(), llm=None, cache_root=tmp_path)
    second = run_research(MICRO, "de", settings, context={"question": MICRO}, transport=web.transport(), llm=None, cache_root=tmp_path)
    assert {item["method"] for item in second.diagnostics["retrieval"]} == {"cache"}
    assert first.facts[0]["claim"] == second.facts[0]["claim"] and len(web.requests) > requests
    stored = next(tmp_path.glob("*.json")).read_text("utf-8")
    assert "<html" not in stored and "cookie" not in stored.casefold()


@pytest.mark.parametrize(("url", "meta", "expected"), [
    ("https://www.nasa.gov/mars", None, "government"),
    ("https://www.uni-heidelberg.de/x", None, "academic"),
    ("https://www.ox.ac.uk/x", None, "academic"),
    ("https://journals.example.com/article/1", {"doi": "10.1000/xyz", "retrieved": True}, "primary_research"),
    ("https://www.stiftung-mauer-museum.de/geschichte", None, "institutional"),
    ("https://de.wikipedia.org/wiki/Mars", None, "reference"),
    ("https://www.gutefrage.net/frage/mars", None, "user_generated"),
    ("https://www.tagesnachrichten.de/x", None, "journalism"),
    ("https://random-blog.example/x", None, "unknown"),
])
def test_source_classification_is_transparent(url, meta, expected):
    result = classify_source(url, meta=meta)
    assert result.source_type == expected and result.reasons


def test_first_party_only_where_routing_expects_it():
    assert classify_source("https://support.tiktok.com/de/x", question_terms={"tiktok"}, first_party_allowed=True).source_type == "first_party"
    # A brand sharing the planet's name is not an authority on the planet.
    assert classify_source("https://www.mars.com/x", question_terms={"mars", "himmel"}).source_type != "first_party"


def test_routing_by_question_domain():
    assert route_question("Warum ist der Himmel auf dem Mars rot?").domain == "science"
    assert route_question("Warum wurde die Berliner Mauer gebaut?").domain == "history"
    assert route_question("Warum streiken die Lokführer heute?").domain == "current_events"
    assert route_question(MICRO).domain in {"technology", "everyday", "science"}
    assert registrable_domain("https://news.bbc.co.uk/a") == "bbc.co.uk"


# ---------------------------------------------------------------------------
# The free path (no Brave key): encyclopedia search -> article -> cited primary sources
# ---------------------------------------------------------------------------

MAUER = "Warum wurde die Berliner Mauer gebaut?"
MAUER_ARTICLE = """Die Berliner Mauer war ein Grenzbefestigungssystem der DDR, das von 1961 bis 1989 bestand.
== Vorgeschichte ==
Bis 1961 verließen rund 2,7 Millionen Menschen die DDR über die offene Sektorengrenze in Berlin nach West-Berlin.
== Bau ==
Die DDR-Führung ließ die Berliner Mauer am 13. August 1961 errichten, um die Massenflucht ihrer Bürger in den Westen zu stoppen.
Offiziell bezeichnete die DDR die Berliner Mauer als antifaschistischen Schutzwall gegen den Westen.
== Literatur ==
Ein Buch über die Mauer mit vielen Seiten und noch mehr Fußnoten erschien in Berlin."""
ARCHIVE = "https://www.bundesarchiv.de/mauerbau-1961"
MIRROR = "https://web.archive.org/web/2010/https://example.org/mauer"


def _mauer_web() -> FakeWeb:
    web = FakeWeb()
    web.wiki_search = {"Berliner Mauer": [{"title": "Berliner Mauer", "snippet": "Die <span>Berliner Mauer</span> war ein Grenzbefestigungssystem"}]}
    web.wiki_extracts = {"Berliner Mauer": MAUER_ARTICLE}
    web.wiki_links = {"Berliner Mauer": [MIRROR, "https://www.youtube.com/watch?v=1", ARCHIVE, "https://blog.example.com/mauer"]}
    web.page(ARCHIVE, html_page("Der Mauerbau 1961", [
        "Am 13. August 1961 begann die DDR mit dem Bau der Berliner Mauer, um die Abwanderung von Arbeitskräften in den Westen zu beenden.",
        "Die anhaltende Fluchtbewegung gefährdete aus Sicht der SED-Führung die wirtschaftliche Existenz der DDR.",
    ], site="Bundesarchiv", schema="GovernmentOrganization"))
    return web


def test_free_path_reaches_cited_primary_sources():
    web = _mauer_web()
    run = run_research(MAUER, "de", research_settings(), context={"question": MAUER}, transport=web.transport(), llm=None, cache_root=None)
    assert run.package["route"]["domain"] == "history"
    used = {source["url"]: source for source in run.package["source_summary"]["sources"]}
    assert ARCHIVE in used and used[ARCHIVE]["source_type"] == "government"
    assert MIRROR not in web.requests and not any("youtube" in url for url in web.requests)
    core = _role(run, "core_answer")[0]
    assert "zu stoppen" in core["claim"] or "zu beenden" in core["claim"]
    assert core["independent_sources"] == 2 and core["verification"] == "supported"
    # Literature sections and the encyclopedia's own boilerplate are not evidence.
    assert all("Fußnoten" not in item["text"] for item in run.package["evidence"])
    assert run.package["status"] == "sufficient" and run.package["explanation_spine"]["status"] == "complete"
    assert any(fact["research_role"] == "number" and "2,7 Millionen" in fact["claim"] for fact in run.facts)


def test_sentence_shaped_queries_fall_back_to_the_subject_noun():
    web = FakeWeb()
    web.wiki_search = {"Mikrowelle": [{"title": "Mikrowellenherd", "snippet": "Mikrowelle Essen erwärmt"}]}
    web.wiki_extracts = {"Mikrowellenherd": (
        "Ein Mikrowellenherd erwärmt Essen mit Mikrowellen, die vor allem Wassermoleküle in Schwingung versetzen.\n"
        "Die Mikrowellen dringen nur wenige Zentimeter in das Essen ein, deshalb bleibt die Mitte großer Portionen länger kalt."
    )}
    run = run_research(MICRO, "de", research_settings(), context={"question": MICRO}, transport=web.transport(), llm=None, cache_root=None)
    calls = run.diagnostics["discovery"]["calls"]
    assert [call["status"] for call in calls if call["provider"] == "wikipedia"][:2] == ["no_hits", "ok"]
    assert "Mitte" in _role(run, "core_answer")[0]["claim"]
