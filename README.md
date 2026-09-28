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
