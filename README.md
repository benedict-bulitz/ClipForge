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

## YouTube Learning Loop

ClipForge can upload a finished export to your own YouTube channel **privately**, schedule it
only when you choose a date and time, and read back real YouTube Analytics (including the raw
audience-retention curve) mapped onto the rendered scenes. It never publishes automatically and
never changes generation rules from performance data.

1. In Google Cloud, enable **YouTube Data API v3** and **YouTube Analytics API**, configure the
   OAuth consent screen (add yourself as a test user while in testing) and create an OAuth client.
   Register `http://localhost:8000/api/youtube/oauth/callback` as its redirect URI.
2. Add the client in **Settings → Integrations → YouTube** (or `YOUTUBE_OAUTH_CLIENT_ID` /
   `YOUTUBE_OAUTH_CLIENT_SECRET` in `.env`) and press **Connect**. Requested scopes:
   `youtube` (upload, status and `publishAt` scheduling) and `yt-analytics.readonly`.
3. Export a project, then use **Upload privately** in its YouTube section.

Note: YouTube restricts videos uploaded through unverified API projects (created after
28 July 2020) to private viewing until the project passes YouTube's API audit, so scheduled
publication only takes effect for an audited project. Studio's "Stayed to watch" is not exposed
by the API and is stored as unavailable; it can be imported manually with provenance.

Read-only diagnostic (no secrets are printed; `--live` reads fresh data without saving it):

```bash
cd apps/api && PYTHONPATH=. ../../.venv/bin/python scripts/youtube_diagnostics.py <project_id>
```

Due analytics snapshots (~1 h, 6 h, 24 h, 72 h, 7 d after publication) are taken by
**Refresh analytics** or `POST /api/youtube/analytics/sync-due`, which is safe to call from cron.

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

See [architecture notes](./docs/ARCHITECTURE.md) for state flow and the provider-backed production slices.
