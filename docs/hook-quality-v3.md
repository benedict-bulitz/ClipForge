# Hook Quality V3 — the first one to three seconds

Branch: `cloud/hook-quality-v3` (base `test` @ `c9bf018`). V3 extends the existing Triple
Hook V2 authority (`verbal_hook` → `triple_hook`); it does not replace it and adds **no
provider call**. New module: `apps/api/clipforge/hook_quality.py`.

## 1. Current hook pipeline (audited)

| Stage | Where | What it does |
|---|---|---|
| Research → facts | `research.research_topic` (Research V2) | Sourced facts; `verification` + `sources` decide which numbers/trends a hook may say |
| Director plan | `ai.plan_with_openai` (Terra, medium) | Facts, script blocks incl. a planner hook, `selected_hook_strategy`, visual intents |
| Story arc / payoff | `story_arc.safe_story_arc`, `pipeline._safe_payoff_plan` | Primary answer, final payoff, `withhold_answer`, `hook_must_not_reveal`, hook-safe facts |
| Body | `pipeline._generate_body_with_v2_or_fallback` (Script Writer V2 + review) | Final body; `order_blocks_for_reveal` keeps a protected answer behind its dependencies |
| Hook context | `verbal_hook.hook_context` | Question/topic words, arc words, protected label, sides, body sentences, `strategy_signals` |
| Hook generation | `pipeline._generate_hook_candidates` → `ai.generate_hook_candidates_with_openai` | **1 call** (Terra, reasoning `low`, 3,200 tokens; one retry only on truncated JSON) → 3–5 complete triples (verbal, structured visual, on-screen) |
| Candidate normalisation | `triple_hook.normalise_candidate` | Documented strategies only; payoff fact bound by id or wording |
| Deterministic checks | `triple_hook.assess_candidate` + `verbal_hook.assess_verbal` + `hooks.hook_issues` | Reveal/leak, clickbait, meta, unsupported numbers/trends/causes, mechanism-first, body duplication, standalone grammar, spoken simplicity, visual vagueness/feasibility, on-screen validity, cross-modal redundancy |
| Top-up | `triple_hook.deterministic_candidates` | Research-derived verbal hooks paired with planner visuals when fewer than 4 provider candidates pass |
| Judge | `ai.judge_triple_hooks_with_openai` | **1 call** (Terra, 1,600 tokens) when ≥ 2 eligible; 0.4 deterministic + 0.6 judge blend; safety dims only lowered; vetoes |
| Fallback | `verbal_hook.select_verbal` → `triple_hook.fallback_plan` | Strong → approximate strategy fit → first body sentence / the question / topic question |
| Final narration | `pipeline._hooked_blocks`, `_apply_selected_hook`, `enforce_selected_hook` | Verbal hook is block 1 (TTS is the timing authority); later stages may not replace it |
| Visual hook | `pipeline._aligned_visual_intents` → `visual_director.plan_scene_strategy` | Hook visual drives the opening scene's queries, verification, crop and generation prompt |
| On-screen text | `triple_hook.hook_overlay_spec` → renderer `label` overlay | The only overlay of the hook window; omitted rather than truncated |
| Render check | `final_critic._hook` | Drawn, readable, not duplicating narration/captions, no reveal, no dead opening |

## 2. Weaknesses found

* "Did you know" / "Hast du dich je gefragt" was only a soft penalty (`generic_opener`);
  other setup formulas ("Heute schauen wir uns an", "Viele Menschen fragen sich", "Let's
  talk about") were not detected at all.
* No notion of *when* the substance arrives: a hook whose subject appears at word 9
  scored like one that opens with it.
* Question restatement was caught only when the hook was (almost) the question itself;
  a statement form of the question, or the question plus "the answer will surprise you",
  passed.
* Placeholder openings ("Etwas Seltsames passiert", "Something strange happens") and
  weak link verbs ("spielt eine Rolle") were not scored.
* A flat statement of the primary answer was only a soft code when the answer was not
  withheld.
* Visual intrigue rewarded *filling fields* (`0.3 + 0.1 × filled`), not instant
  readability, motion, crop safety or avoiding generic stock.
* The deterministic opening visual was the planner visual with the **most word overlap
  with the voice** — i.e. the one that merely illustrates.
* Paraphrases survived when they carried different strategy labels.
* On-screen text that restated the question was not penalised.

## 3. V3 quality model

Eight scores per opening (persisted on the selected plan as `triple_hook.hook_quality`):

| Score | Source |
|---|---|
| A immediate clarity | new `first_second_clarity`: topic anchor inside the first-second window |
| B curiosity gap | `curiosity` (capped for empty teasers) |
| C specificity | `useful_information` |
| D novelty | `insight`, capped when nothing goes beyond the question |
| E information density | new `information_density`: content per spoken word, minus setup/teaser/weak-verb filler |
| F complementarity | `complementarity` (+ on-screen role, − restated question) |
| G payoff integrity | `payoff_alignment` (+ `payoff_mismatch`, `early_payoff` gates) |
| H scroll-stop | new `scroll_stop`: something concrete and new before the swipe decision |

Windows come from the narration rate (`SPEAKING_RATE_WPM = 165`, now defined once in
`hook_quality`): first-second window = 1.5 s ≈ 4 words, opening window = 3 s ≈ 8 words.
New triple dimension `visual_clarity`; `visual_intrigue` is recomputed from the first frame.
New dimensions are deterministic only (the judge schema is unchanged).

## 4. Gates (hard only where confident)

| Gate | Hard when | Otherwise |
|---|---|---|
| `setup_opener` | always (non-emergency) | — |
| `empty_teaser` | nothing beyond the question | soft code, curiosity capped |
| `question_restatement` | covers the question and adds nothing (numbers and negation count) | `question_led_opening` soft |
| `vague_opening` | no concrete anchor in the opening window and no specific content | soft, clarity −0.3 |
| `early_payoff` | says most (≥ 60 %) of the primary answer / final payoff flatly, with no contrast, number, question or challenge | a reveal that opens a new question, or names only part of the answer, stays |
| `overstates_certainty` | the hook states as fact what every research clause it rests on only hedges (may, hypothesis, vermutlich, könnte …) | a hook with its own hedge, a question, or a claim another fact states with certainty stays |
| `delayed_specificity`, `slow_start`, `weak_verb`, `abstract_opening` | never | score penalties |
| visual `visual_off_topic`, `visual_cluttered`, `visual_crop_risk`, `visual_generic_stock`, `visual_merely_illustrates` | never | first-frame score |
| on-screen `on_screen_restates_question`, `on_screen_hard_to_read` | never | quality/complementarity |

The user's question as emergency fallback is never rejected by V3. A user's own edited hook
is unaffected (`verbal_still_valid` only checks safety codes).

### Uncertainty preservation (Research V2 compatibility)

Research V2 keeps hypotheses hedged. `verbal_hook.overstated_certainty` (one gate in
`assess_verbal`, so deterministic, planner, provider and fallback hooks alike; never relaxed)
compares each unhedged hook clause with the research clauses it rests on
(`hooks.uncertainty_scopes`): a hedge's scope ends at a sentence or an adversative turn
("…, but X was first seen in 1950"), and a modal qualifies only what follows it ("X may cause
Y": the cause, not X). It rejects only when the hook uses the hedged part and no certain
clause carries the same claim. `hooks._grounded_insight` no longer cuts a hedge off a long
claim. A user's own edited hook is not re-judged by this gate.

## 5. Diversity

`hook_quality.opening_move` classifies each candidate (number, challenge, visual reveal,
contrast, contradiction, consequence, mystery, counterintuitive, fact). Paraphrases are
dropped regardless of strategy label (≥ 80 % word overlap, or ≥ 65 % with the same move and
strategy). When the eligible provider candidates use a single move, deterministic research
candidates are added (no call). `selection.opening_moves` records the spread.

## 6. Cost

No new call. Generation and judge prompts gained ~1,000 characters of stable instruction
text (cacheable prefix). Persisted state grows by four dimensions per candidate plus one
compact report.
