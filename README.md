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

The default `CLIPFORGE_AI_MODE=local` needs no keys. It creates a real persisted ProjectState with a deterministic development planner. To use the OpenAI director, copy `.env.example` to `.env`, set `CLIPFORGE_AI_MODE=openai`, and add `OPENAI_API_KEY`.

For PostgreSQL, Redis, and the worker:

```bash
docker compose up --build
```

## Verify

```bash
npm test
npm run build
```

## API

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/api/projects` | Create a project from one prompt |
| `GET` | `/api/projects/{id}` | Load the current revision |
| `POST` | `/api/projects/{id}/edits` | Apply a natural-language change |
| `POST` | `/api/projects/{id}/undo` | Move to the previous revision |

See [architecture notes](./docs/ARCHITECTURE.md) for state flow and the provider-backed production slices.

