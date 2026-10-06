# ClipForge

ClipForge turns one plain-language idea into a structured shortform video project. It decides whether to research, keeps only the essential information, writes a compact script, estimates the natural duration, creates visual beats, and stores the result as an editable revision. The user can then direct changes in chat.

This repository contains the first working product milestone from the supplied [blueprint](./docs/BAUPLAN.txt):

- A polished Next.js 16 interface with an optional advanced panel.
- A FastAPI project API with SQLAlchemy 2 and Pydantic v2.
- Immutable original prompts and append-only project revisions.
- Automatic intent, research, information, script, duration, storyboard, caption, music, and timeline planning.
- Chat edit interpretation, selective dependency invalidation, content hashes, and undo.
- PostgreSQL, Redis, Celery, Alembic, and Docker foundations.
- Explicit readiness states for research, media, voice, rendering, and QC providers.

## Run locally

Requirements: Node.js 22+ and Python 3.11+.

```bash
./scripts/bootstrap.sh
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The API and its interactive docs run at [http://localhost:8000/docs](http://localhost:8000/docs).

Verified final MP4 exports are saved to the repository-local `Downloads/` directory. After a
successful export, ClipForge keeps project metadata while removing that project's generated media.

Bootstrap installs ClipForge's local Faster Whisper word aligner in `.venv`. Its lightweight `tiny`
model downloads into the Hugging Face cache only when narration is aligned for the first time. To
upgrade an existing checkout, run `./.venv/bin/pip install -e "apps/api[dev]"`.
Caption text always comes from the final narration script; the aligner only supplies word timing.
If its timing cannot be mapped onto the script reliably, captions fall back to phrase timing.

The default `CLIPFORGE_AI_MODE=local` needs no keys. It creates a real persisted ProjectState with a deterministic development planner. To use the OpenAI director, copy `.env.example` to `.env`, set `CLIPFORGE_AI_MODE=openai`, and add `OPENAI_API_KEY`.

For PostgreSQL, Redis, and the worker:

```bash
docker compose up --build
```

## YouTube publishing and Learning Loop

When a video is rendered, **Upload to YouTube** is the main action: ClipForge uploads its
canonical final render directly (the narrated render, or the one music mix next to it) — no
export or download step. "Save local MP4" remains in the project's overflow menu.

The publishing sheet asks for everything YouTube needs before any byte is sent:
thumbnail (a ClipForge cover, your own JPEG/PNG, or explicitly YouTube's frame), title,
description and tags with live limits, **Made for kids** (explicit Yes/No, always sent as
`status.selfDeclaredMadeForKids`), **altered or synthetic content**
(`status.containsSyntheticMedia`, with an explained suggestion), visibility, and a schedule with
date, 24-hour or 12-hour time (per locale) and an explicit IANA time zone resolved DST-safely.
Advanced settings (all verified against Google's discovery document): category (fetched from
YouTube), language, license, embedding, public statistics, notify subscribers, recording date
and paid promotion. Studio-only settings (chapters, comments, Shorts remixing, featured places,
age restriction, caption certification, monetization, …) are listed, never faked.
Upload defaults live in **Settings → Integrations → YouTube**; compliance answers default to
"ask every time".

Deleting a project is upload-aware: a project that was never uploaded is deleted completely;
a successfully uploaded project loses all local media but keeps its compact learning record
(upload mapping, production fingerprint, analytics snapshots, raw retention), browsable under
**Learning History** (`/learning`). If an upload's outcome cannot be verified, deletion fails
closed until you retry or explicitly confirm. ClipForge never deletes YouTube videos.

Setup:

1. In Google Cloud, enable **YouTube Data API v3** and **YouTube Analytics API**, configure the
   OAuth consent screen (add yourself as a test user while in testing) and create an OAuth client.
   Register `http://localhost:8000/api/youtube/oauth/callback` as its redirect URI.
2. Add the client in **Settings → Integrations → YouTube** (or `YOUTUBE_OAUTH_CLIENT_ID` /
   `YOUTUBE_OAUTH_CLIENT_SECRET` in `.env`) and press **Connect**. Requested scopes:
   `youtube` (upload, status, thumbnails, scheduling) and `yt-analytics.readonly`.

Note: YouTube keeps videos uploaded through unverified API projects (created after
28 July 2020) private until the project passes YouTube's API audit, so ClipForge offers only
Private and Schedule until you mark the project as audited in the upload defaults. Custom
thumbnails require a verified YouTube channel. Studio's "Stayed to watch" is not exposed by the
API and is stored as unavailable; it can be imported manually with provenance.

After upload, YouTube's own answer (`videos.list`) is the current truth: the Results page
reconciles it when it opens (freshness-gated) and polls only around upload and publish time, with a
bounded backoff. The requested schedule is kept as history; if YouTube cannot be reached, the last
confirmed state is shown as stale and never reverted. Live views/likes/comments come from the Data
API; detailed analytics (engaged views, watch time, retention) are processed by YouTube later and
are shown as "Processing on YouTube" until they arrive.

Read-only diagnostic (no secrets are printed; `--live` reads fresh data without saving it):

```bash
cd apps/api && PYTHONPATH=. ../../.venv/bin/python scripts/youtube_diagnostics.py <project_id>
```

Due analytics snapshots (~1 h, 6 h, 24 h, 72 h, 7 d after publication) are taken by
**Refresh analytics** or `POST /api/youtube/analytics/sync-due`, which is safe to call from cron.

## Live topic suggestions (Topic Intelligence V2)

Three modes: type your own question (manual, unchanged), pick one of three suggestions
(assisted), or **Generate automatically** (Full Auto: the strongest eligible question is started
through the normal generation entry point - or nothing starts when no question is strong enough).
V2 adds evergreen discovery with real Wikipedia pageview evidence, explicit curiosity/payoff
gates, evidence-backed labels (Trend / Aktuell / Im Kommen / Zeitlos) with freshness TTLs,
semantic dedupe and confidence-aware ranking (`ti-score-v7`, `semantic-curator-v3`). Audit,
design, budgets and the Real-Mac retest: [docs/topic-intelligence-v2.md](./docs/topic-intelligence-v2.md).
The V1 notes below still describe the shared runtime, caching and curator mechanics.

The three suggestion chips under the prompt are live German Topic Intelligence questions.
Clicking one only copies it into the prompt (generation still starts with the normal
**Generate** button) and immediately replaces that one chip from a prefetched reserve; the other
two stay. **Neue Vorschläge** replaces all three without touching the prompt. Visible chips never
change on their own: the set is persisted in the browser, and only a hidden reserve is refilled in
the background. Discovery also warms up when the API starts, so suggestions are usually ready
before the page opens; video generation never waits for it.

- **Sources** (official APIs only, each behind one small `TopicSource` interface): German
  Wikipedia pageviews (recent interest vs. the article's own median), YouTube's German
  most-popular chart for Education / Science & Technology (when a channel is connected; each
  video is compared with its own channel's recent uploads, never by absolute views), and
  Brave News for Germany (when the Brave key is configured). Competition is a bounded YouTube
  `search.list` estimate for the strongest candidates only.
- **AI curation** (since `semantic-curator-v1` / `ti-score-v5`): one combined worker-model call per batch of
  up to 20 raw topics creates the question *and* judges it - no separate rewrite and validation
  requests, at most 3 requests (60 topics) per pool. The curator sees each topic with its source
  evidence (titles, descriptions, kind) and, when there is one, the locally extracted question as a
  hint. It turns a statement headline into a question only when the evidence supports it
  ("Brustkrebsvorsorge in Zukunft mit einer einfachen Blutprobe?" → "Kann Brustkrebs künftig mit einem
  einfachen Bluttest erkannt werden?") and never invents mechanisms, numbers, causes or contradictions
  (an ungrounded question is rejected as `semantic_unsupported_premise`). It scores self-contained
  clarity, clear factual payoff, universal 12+ relevance, freedom from prior knowledge, natural spoken
  German and knowledge-short fit (0-10 each) plus issue codes (`unexplained_metaphor`,
  `rhetorical_or_opinion`, `too_narrow_audience`, `demographic_subgroup_only`, ...). Understandable is
  not the same as universal: product, brand, subgroup ("Was tun Männer ...?"), hobby or specialist
  questions do not pass. Any issue or any dimension below 6 rejects; `scoring.py` still decides.
  Results are cached per topic evidence, curator version and model. Topic AI is enabled by
  `OPENAI_API_KEY` alone - independent of `CLIPFORGE_AI_MODE`, which keeps controlling video generation
  only (disable with `TOPIC_SEMANTIC_VALIDATION=false`).
- **Without a key** (`tq3` local questions): a deterministic step keeps real questions ("Beziehung:
  Wie gesund ist die Liebe?" → "Wie gesund ist die Liebe?"), turns indirect clauses and
  "Darum/So/Das passiert"-headlines into direct questions, strips channel suffixes, hashtags, ALL-CAPS
  emphasis and exclamation marks, and uses a concrete template only where the encyclopedia says what a
  subject is - never a generic "Was steckt eigentlich hinter X?". Strict local rules then apply: only
  why/how/what-if questions with a fully accessible premise and no clickbait-styled source - fewer
  suggestions, never unvetted ones.
- **Deterministic checks** stay a prefilter, not the bottleneck: calendar pages are dropped before any
  AI call (reported as `prefiltered`), and every question is checked for English, a missing question
  mark, an embedded answer, clickbait and unsupported numbers.
- **Raw-pool backfill**: discovery ranks the order of work, it is not a gate. Topics are evaluated in
  batches through the already-discovered raw pool until 9 candidates clear every gate or 60 topics
  were evaluated / 3 AI requests used - without new provider calls.
- **Curator reliability**: the AI budget (3 requests per pool) goes to the most promising raw
  topics first - ordered by cheap evidence only (demand, a well-formed question already in a title,
  a universal subject, grounding evidence, audience prior, novelty; minus brand/product/institution
  wording, obscure entities, poor-fit niches, weak question shapes and repeats of earlier videos).
  Each request carries 10 topics (`TOPIC_CURATOR_BATCH_SIZE`; v2's 20 per request timed out) at low
  reasoning effort, like every other ClipForge structured call. A failed request marks only its own
  topics as *not evaluated* (`curator_failed`) - never low quality: they are retried first, in a
  smaller batch after a timeout, within the same budget, and judged topics of other batches are kept.
  Topics the curator could not judge are never served as unvalidated local filler; Home shows fewer
  suggestions instead. `topic_diagnostics.py --status` lists every request (size, seconds, tokens,
  ok/timeout), the curation order and what stayed unevaluated; `--curated` lists every curated
  candidate with all dimensions, short-worthiness, issue codes and the gates that rejected it.
- **Short-worthiness** (`ti-score-v6`, `semantic-curator-v2`): clear, factual and broad is not enough
  - a default suggestion must make a strong short. The curator also rates curiosity strength, payoff
  specificity, reveal potential, concreteness and single-question focus; together with knowledge-short
  fit and visual potential they form the `short_worthiness` signal (weight 0.13). One core question
  only: multi-part questions ("X, und welchen Anteil hat Y?"), "Welche Faktoren/Gründe/Tipps ..." list
  questions and population survey/measurement questions are rejected; generic advice, broad
  overviews and questions without a clear reveal only rank lower. Short-worthiness below 0.45 rejects
  as `weak_short_concept`, and a trend spike counts only as much as the concept is short-worthy -
  demand helps a strong short but cannot rescue a weak one. No domain is penalized: a health or
  social topic wins with a concrete, surprising reveal. The 12+ and grounding gates are unchanged.
- **Availability**: an accepted candidate stays available until the user picks or dismisses it; a
  refill that finds nothing new never discards the shown chips. With only 1-2 strong candidates Home
  shows exactly those ("Gerade nur 2 starke Vorschläge.").
- **Bounded discovery** (`topic_intelligence/runtime.py`): nothing waits forever. Each provider fetch
  and competition probe runs under a 30 s wall-clock bound (a hung provider is marked `timeout`, the
  others continue, and it is not retried within the same refresh); each curator request has a 60 s client timeout without retries plus a
  wall-clock guard, and no new request starts after 150 s (the rest falls back to strict local rules).
  Discovery holds no database transaction while waiting on the network. The single-flight lock has an
  owner: a discovery (warm-up or request) holding it longer than 300 s is abandoned, the warm-up is
  marked failed, and "Neue Vorschläge" can retry at once; a failed discovery ends as a `failed` run with
  its error and never hides the last complete pool. Home waits for chips at most 180 s, then says
  "Themenvorschläge konnten nicht geladen werden." and keeps "Neue Vorschläge" as the retry.
- **Universal 12+ gate** (since `ti-score-v3`): a question whose premise needs prior knowledge (title
  context, a brand or multi-word name, product-generation news, an institution or one specific
  event, a date or identifier, or an obscure entity leading the question) is rejected as
  `requires_prior_knowledge`; a niche topic passes only when reframed around a universal phenomenon.
- **Novelty** compares against projects, queued requests, uploaded videos and the Learning
  Archive with a light German-aware similarity (compounds, umlauts, synonyms), so rephrasings of an
  earlier video are rejected.
- **Scoring** (V1 history; V2 weights and gates: see docs/topic-intelligence-v2.md): one versioned authority (`topic_intelligence/scoring.py`, now `ti-score-v7`) with
  documented weights. Mass-audience quality (suitability, broad appeal, accessibility without prior
  niche knowledge, question form) carries the score; a trend spike counts only as much as its
  corroboration and the topic's quality allow; date pages, identifiers, isolated events, acronyms,
  proper-noun compounds and generic "Was steckt hinter X?" wrappers are penalized unless there is
  exceptional evidence; candidates below the quality floor are never served as filler (one broader
  discovery pass is tried instead); near-equal candidates are diversified by subject and question
  structure. Missing data (e.g. no own analytics yet) is neutral, never zero, and only lowers the
  confidence. Override weights with `TOPIC_SCORE_WEIGHTS='{"trend": 0.25}'` (the score
  version then records the override).
- **Cost control**: normalized provider results are cached (2–24 h), the scored pool is reused for
  `TOPIC_POOL_TTL_MINUTES` (45), refreshes are single-flight, and each refresh has a hard YouTube
  quota budget (`TOPIC_YOUTUBE_QUOTA_BUDGET`, 400 units; at most `TOPIC_YOUTUBE_SEARCH_PROBES`=2
  searches).
- **Handoff**: a question taken from a chip goes through the same `POST /api/generation-jobs` as a
  typed question. `topic_source` (`manual` | `topic_intelligence`), the candidate id, score version, score
  breakdown, signals used and selection time are stored with the request, the project state and
  the production fingerprint for later analytics learning.

If every discovery source fails and no chips are cached, Home says "Topic discovery is temporarily
unavailable." and the manual prompt keeps working; if only some fail, the remaining sources are used with lower
confidence. `GET /api/topic-intelligence/status` shows pool and cache freshness.

If Home shows no chips, it says why: "Themenvorschläge werden gesucht…" while discovery runs, only
the questions that cleared the quality floor plus a note when there are fewer than three (the floor is
never lowered to fill slots), or "Themenvorschläge konnten nicht geladen werden." when discovery
failed or timed out. To see which state applies:

```bash
curl -s http://localhost:8000/api/topic-intelligence/status | python3 -m json.tool   # includes the live warm-up state
cd apps/api && PYTHONPATH=. ../../.venv/bin/python scripts/topic_diagnostics.py --status --live
```

While discovery runs, `discovery` shows `discovery_stage` (`provider_cache_read`, `provider_fetch`,
`merge_raw_topics`, `prefilter`, `curation_batch`, `curator_request`, `scoring`, `competition_probe`,
`persistence`), `active_provider`, `stage_started_at`, `elapsed_seconds`, the lock owner, the stage log
with durations and every timeout event.

`diagnosis` is one of `discovery_running`, `discovery_failed`, `discovery_timed_out`, `warmup_failed`, `no_pool_yet`, `provider_failure`,
`stale_pool_other_version`, `pool_expired_refreshes_on_next_request`, `question_transformation_failed`,
`quality_floor_rejected_all`, `prior_knowledge_rejected_all`, `semantic_rejected_all`,
`local_strict_rejected_all`, `no_usable_candidates`,
`fewer_than_three_candidates` or `ok`, next to per-source status, pool size,
accepted/rejected candidates with rejection reasons, the score version and cache freshness.

Tuning diagnostics (developer only, read-only, no external calls):

```bash
cd apps/api && PYTHONPATH=. ../../.venv/bin/python scripts/topic_diagnostics.py --shown 9 --rescore
```

prints every served candidate with its sources, all score components, penalties, the quality gate,
final score and rejection reasons (`--rescore` also scores persisted candidates with the current
version). The same data is available at `GET /api/topic-intelligence/diagnostics` outside
production.

## Cancelling a running generation

The Video Queue shows **Abbrechen** on the running job (queued jobs keep **Remove**). The job
turns `cancelling` at once ("Wird abgebrochen…") and `cancelled` ("Abgebrochen") when the worker
reaches its next checkpoint: every progress event (stage, scene, asset, render segment), before
rendering, before a finished render is saved, and at completion. A render/FFmpeg process that
ClipForge started for the job is terminated (killed only after a short grace period) and reaped;
other processes are never touched, and a provider request that cannot be interrupted simply
returns first. `cancelled` is terminal - never `failed` or `completed`, never resumed after a
restart (a restart finishes a pending cancel). The project, its question and settings and all saved
revisions are kept; only directories the cancelled run created and never saved
(`renders/vN`, `audio-layers/vN`) are removed. The next queued job starts right after.

```bash
curl -X POST http://localhost:8000/api/generation-jobs/<job-id>/cancel   # idempotent; 409 for queued/finished jobs
```

## Verify

```bash
npm test
npm run build
```

The API tests run from the repository virtualenv installed by bootstrap:

```bash
./.venv/bin/pytest apps/api/tests
```

## API

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/api/projects` | Create a project from one prompt |
| `GET` | `/api/projects/{id}` | Load the current revision |
| `POST` | `/api/projects/{id}/edits` | Apply a natural-language change |
| `POST` | `/api/projects/{id}/undo` | Move to the previous revision |
| `POST` | `/api/topic-intelligence/suggestions` | Ranked chip suggestions, excluding what is shown |
| `POST` | `/api/topic-intelligence/auto` | Full Auto: the strongest eligible question, or `no_strong_candidate` |
| `POST` | `/api/topic-intelligence/next` | Propose the single next topic (reuses the fresh pool) |
| `POST` | `/api/topic-intelligence/candidates/{id}/skip` | Skip a candidate and propose the next |
| `GET` | `/api/topic-intelligence/status` | Discovery pool and cache freshness |

See [architecture notes](./docs/ARCHITECTURE.md) for state flow and the provider-backed production slices.
