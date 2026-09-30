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

## Live topic suggestions (Topic Intelligence V1)

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
- **Topic → question** (`tq3`): with `CLIPFORGE_AI_MODE=openai`, one worker-model call rewrites a
  batch of up to 20 raw topics into natural German questions and rates their suitability. Locally,
  a deterministic step keeps real questions ("Beziehung: Wie gesund ist die Liebe?" → "Wie gesund ist
  die Liebe?"), turns indirect clauses and "Darum/So/Das passiert"-headlines into direct questions,
  strips channel suffixes, hashtags, ALL-CAPS emphasis and exclamation marks, and uses a concrete
  template only where the encyclopedia says what a subject is - never a generic
  "Was steckt eigentlich hinter X?". Every question is validated (German, a real question, no
  embedded answer, no clickbait, no unsupported numbers).
- **Raw-pool backfill**: discovery ranks the order of work, it is not a gate. Topics are evaluated in
  batches through the already-discovered raw pool until 9 candidates clear every gate or 60 topics
  were evaluated (at most 3 rewrite requests with OpenAI) - without new provider calls.
- **Universal 12+ gate** (`ti-score-v3`): a question whose premise needs prior knowledge (title
  context, a brand or multi-word name, product-generation news, an institution or one specific
  event, a date or identifier, or an obscure entity leading the question) is rejected as
  `requires_prior_knowledge`; a niche topic passes only when reframed around a universal phenomenon.
- **Novelty** compares against projects, queued requests, uploaded videos and the Learning
  Archive with a light German-aware similarity (compounds, umlauts, synonyms), so rephrasings of an
  earlier video are rejected.
- **Scoring**: one versioned authority (`topic_intelligence/scoring.py`, `ti-score-v3`) with
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
never lowered to fill slots), or "Topic discovery is temporarily unavailable." when every source
failed. To see which state applies:

```bash
curl -s http://localhost:8000/api/topic-intelligence/status | python3 -m json.tool   # includes the live warm-up state
cd apps/api && PYTHONPATH=. ../../.venv/bin/python scripts/topic_diagnostics.py --status
```

`diagnosis` is one of `discovery_running`, `warmup_failed`, `no_pool_yet`, `provider_failure`,
`stale_pool_other_version`, `pool_expired_refreshes_on_next_request`, `question_transformation_failed`,
`quality_floor_rejected_all`, `prior_knowledge_rejected_all`, `no_usable_candidates`,
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
| `POST` | `/api/topic-intelligence/next` | Propose the single next topic (reuses the fresh pool) |
| `POST` | `/api/topic-intelligence/candidates/{id}/skip` | Skip a candidate and propose the next |
| `GET` | `/api/topic-intelligence/status` | Discovery pool and cache freshness |

See [architecture notes](./docs/ARCHITECTURE.md) for state flow and the provider-backed production slices.
