"""Real-Mac-shaped research fixtures for semantic answer grounding.

Each case reproduces the evidence shape of a Real-Mac network run of Research
V2 (the wrong sentence that was selected, plus the relevant evidence that was
available) on the offline fake web.  Topics are fixtures only; production code
has no topic vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from research_v2_support import FakeWeb, brave_hit, html_page


@dataclass
class Case:
    question: str
    keyword: str  # Brave fixture key (a word every query of the case contains)
    pages: list[tuple[str, str, list[str], dict]] = field(default_factory=list)  # url, title, paragraphs, html kwargs
    snippets: list[tuple[str, str, str]] = field(default_factory=list)  # url, title, snippet (page not retrievable)

    def web(self) -> FakeWeb:
        web = FakeWeb()
        hits = [brave_hit(url, title, paragraphs[0][:100]) for url, title, paragraphs, _ in self.pages]
        hits += [brave_hit(url, title, snippet) for url, title, snippet in self.snippets]
        web.brave = {self.keyword: hits}
        for url, title, paragraphs, kwargs in self.pages:
            web.page(url, html_page(title, paragraphs, **kwargs))
        for url, _title, _snippet in self.snippets:
            web.page(url, "blocked", status=403)
        return web


MICROWAVE = Case(
    "Warum bleibt Essen in der Mikrowelle in der Mitte kalt?",
    "Mikrowelle",
    pages=[(
        # Real Mac shape: how microwaves heat food in general - the parent topic, not the cold centre.
        "https://www.verbraucherinfo.bund.de/mikrowelle",
        "So funktioniert die Mikrowelle",
        [
            "Mikrowellen erwärmen Lebensmittel mithilfe von elektromagnetischen Wellen, die die Wassermoleküle im Essen "
            "in Schwingung versetzen und dadurch Wärme erzeugen.",
            "In der Mitte des Garraums befindet sich oft eine sogenannte tote Zone, in der die Wellen kaum Energie abgeben.",
        ],
        {"site": "Verbraucherinformation"},
    ), (
        "https://www.uni-beispiel.de/physik/mikrowellen-erwaermung",
        "Wie Mikrowellen Lebensmittel erwärmen",
        [
            "Mikrowellen dringen nur wenige Zentimeter tief in das Essen ein, deshalb erwärmt die Mikrowelle zuerst die äußeren Schichten.",
            "Das Innere wird danach nur langsam durch Wärmeleitung von außen erwärmt.",
        ],
        {"site": "Universität Beispiel"},
    )],
    snippets=[(
        "https://kochtipps-sammlung.example/mikrowelle",
        "Mikrowelle: Essen in der Mitte kalt?",
        "Tiefe Speisen verhindern, dass Essen in der Mikrowelle in der Mitte warm wird, deshalb bleibt die Mitte kalt.",
    )],
)

MARS = Case(
    "Warum ist der Himmel auf dem Mars rot?",
    "Mars",
    pages=[
        (
            "https://de.wikipedia.org/wiki/Mars_(Planet)",
            "Mars (Planet)",
            ["Der Mars wird häufig als der rote Planet bezeichnet, weil er am Nachthimmel wie ein orangeroter Stern erscheint."],
            {},
        ),
        (
            "https://www.weltraum-wissen.de/mars-atmosphaere",
            "Die Atmosphäre des Mars",
            [
                "Die dünne Atmosphäre des Mars enthält sehr viel feinen Staub. Weil die schwebenden Staubteilchen das Sonnenlicht streuen, erscheint der Himmel dort tagsüber orangebraun.",
                "Die Staubteilchen sind so klein, dass Winde sie monatelang in der Luft halten.",
            ],
            {},
        ),
    ],
)

BERLIN = Case(
    "Warum wurde die Berliner Mauer gebaut?",
    "Mauer",
    pages=[
        (
            "https://www.berlin-stadtgeschichte.de/ebertstrasse",
            "Die Berliner Mauer an der Ebertstraße",
            # Real Mac shape: construction layout and its result ("sodass"), not why the wall was built.
            ["Beim Bau der Mauer 1961 zog die DDR die Sperranlagen gerade entlang der Ebertstraße, "
             "sodass das Gelände als Zipfel Ost-Berlins abgeschnitten wurde."],
            {},
        ),
        (
            "https://www.zeitgeschichte-online.de/berliner-mauer",
            "Die Berliner Mauer",
            [
                # Real Mac shape: the direct answer refers to the wall only by pronoun.
                "Die Berliner Mauer trennte fast drei Jahrzehnte lang Ost- und West-Berlin. "
                "Gebaut wurde sie 1961, um den Flüchtlingsstrom vom Osten in den Westen zu stoppen.",
                # A consequence of the wall, with the wall inside the cause clause.
                # Real Mac shape: a consequence of the building, with the building inside the "weil" clause.
                "Im Gegenteil: Weil der Mauerbau Freunde und Verwandte in Berlin voneinander getrennt hatte, versuchten besonders "
                "in Ost-Berlin und im Berliner Umland noch viele Menschen, über die Grenzsperren zu fliehen.",
                # Two different metrics of the same history - not a contradiction.
                "Über die Berliner Mauer und die innerdeutsche Grenze flohen bis 1961 rund 3,5 Millionen Menschen aus der DDR.",
            ],
            {},
        ),
        (
            "https://www.mauer-gedenkstaette-beispiel.de/todesopfer",
            "Todesopfer an der Berliner Mauer",
            ["An der Berliner Mauer und der innerdeutschen Grenze wurden mindestens 140 Menschen aus der DDR getötet."],
            {},
        ),
    ],
)

ARGUMENT = Case(
    "Warum fallen uns gute Antworten immer erst nach einem Streit ein?",
    "Streit",
    pages=[(
        "https://www.beziehungs-ratgeber.example/streit",
        "Richtig streiten",
        [
            "Damit uns im nächsten Streit gute Antworten einfallen, hilft schon ein kurzer Gedanke, um einen Streit gar nicht erst entstehen zu lassen.",
            "Streiten ist wichtig, weil Paare dadurch Konflikte offen ansprechen und klären können.",
            # Real Mac shape: conflict-prevention advice with an "indem" clause.
            "Trotzdem kann man fiese Streite umgehen: indem man Kleinigkeiten, die einen stören, gleich anspricht, statt sie zu sammeln.",
            # Real Mac shape: the article's own purpose, phrased with "Grund", "warum" and "damit".
            "Ein weiterer Grund, warum es mir so wichtig ist, dir mit diesem Artikel einen Schritt zur Lösung deiner Konflikte "
            "und damit Antworten zu liefern. Damit du und dein Partner nicht mehr im Streitkreislauf gefangen bleibt.",
        ],
        {},
    )],
)

AI_PAIN = Case(
    "Kann eine künstliche Intelligenz überhaupt Schmerz empfinden?",
    "Intelligenz",
    pages=[(
        "https://www.klinik-technik.example/ki-schmerz",
        "KI in der Schmerztherapie",
        [
            "Eine künstliche Intelligenz analysiert Biosignale, um Schmerz bei Patienten früher zu erkennen.",
            "Die KI misst dabei Herzschlag und Hautleitfähigkeit und meldet starken Schmerz an das Pflegepersonal.",
        ],
        {},
    )],
)

AI_IMAGE = Case(
    "Warum erfindet KI Bilder und Videos nicht völlig neu?",
    "Bilder",
    pages=[(
        "https://www.suchmaschinen-hilfe.example/rueckwaertssuche",
        "Bilder rückwärts suchen",
        [
            "Mit der Google-Rückwärtssuche finden Sie heraus, ob Bilder und Videos von einer KI erstellt wurden, weil die Suche ähnliche Bilder im Netz anzeigt.",
        ],
        {},
    )],
)

KAUGUMMI = Case(
    "Warum kann man Kaugummi kauen ohne dass er zerfällt?",
    "Kaugummi",
    pages=[(
        "https://www.lebensmittel-lexikon.example/kaugummi",
        "Kaugummi",
        [
            "Ein Kaugummi lässt sich lange kauen und zerfällt dabei nicht in kleine Stücke.",
            "Kaugummi besteht aus einer Kaumasse, Zucker oder Süßstoffen sowie Aromen und wird seit über hundert Jahren verkauft.",
            "Ein Kaugummi wird im Durchschnitt etwa zwanzig Minuten lang gekaut, bevor er seinen Geschmack verliert.",
        ],
        {},
    )],
)

CASES = {
    "microwave": MICROWAVE, "mars": MARS, "berlin": BERLIN, "argument": ARGUMENT,
    "ai_pain": AI_PAIN, "ai_image": AI_IMAGE, "kaugummi": KAUGUMMI,
}
