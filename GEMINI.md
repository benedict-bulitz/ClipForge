# CLIPFORGE — MASTER BUILD PROMPT FOR GEMINI

## 0. PURPOSE OF THIS DOCUMENT

You are continuing the development of a project called **ClipForge**.

This document is the authoritative master specification for the project.
Treat it as the single source of truth for architecture, behavior, implementation priorities, UX goals, provider choices, security requirements, and acceptance criteria.

Your job is not merely to give advice.
Your job is to **continue building ClipForge in a structured, production-minded way**.

When making implementation decisions:
1. Preserve existing working functionality.
2. Prefer simple, robust architecture over unnecessary complexity.
3. Avoid breaking changes unless they are required.
4. Keep the app usable locally.
5. Build features in the priority order defined below.
6. Do not reintroduce terminal-only workflows where they can reasonably be handled inside the app.
7. Do not store secrets insecurely.
8. Do not fabricate facts during research or video generation.
9. Do not replace the whole codebase unless absolutely necessary.
10. Prefer incremental, testable changes.

If the current repository differs from this specification, inspect the repository first and adapt carefully.

---

# 1. PRODUCT VISION

ClipForge is a local AI-powered short-form video creation application.

The user should be able to enter only:
- a topic,
- a question,
- or a natural-language video description,

and ClipForge should automatically create a complete short-form video.

Example input:

> Why do airplane windows have rounded corners?

ClipForge should automatically perform:

1. Prompt understanding
2. Research planning
3. Web research
4. Fact validation
5. Hook generation
6. Script generation
7. Automatic duration selection
8. Storyboard creation
9. Media search
10. Media ranking
11. Media download and caching
12. Smart crop to vertical
13. Voice generation
14. Word-level timing
15. Captions
16. Motion / zoom / pan
17. Music and SFX
18. Timeline generation
19. Rendering
20. Technical quality control
21. Project history / revisions
22. Natural-language editing after generation

The target platforms are:

- TikTok
- YouTube Shorts
- Instagram Reels

Default output:

- 1080 × 1920
- 9:16
- 30 FPS
- H.264
- AAC
- MP4

---

# 2. CORE PRODUCT PRINCIPLE

ClipForge must not behave like a slideshow generator.

The visual part should primarily consist of:

- real stock video,
- images,
- historical images,
- relevant close-ups,
- short clips,
- crops,
- pans,
- zooms,
- motion effects,
- captions,
- lightweight overlays,
- fast cuts.

Avoid using generic text cards as the main visual content.

Text cards may only be used when they are intentionally useful as a design element.

---

# 3. AUTOMATIC VIDEO LENGTH

Do NOT force every video into a fixed duration such as 20–30 seconds.

ClipForge should determine the shortest duration that still explains the content properly.

Guiding principle:

> As short as possible, but as long as necessary.

Examples:
- Simple fact: 12–18 seconds
- Medium explanation: 20–35 seconds
- More complex explanation: 35–60 seconds

Still keep the result suitable for short-form content.

The script planner should reduce redundancy and preserve clarity.

---

# 4. CURRENT TECHNOLOGY STACK

Use the existing project architecture where possible.

Preferred stack:

## Frontend
- Next.js
- React

## Backend
- FastAPI
- Python

## Local database
- SQLite

## Main LLM / AI director
- OpenAI

## Search
- Brave Search API
- Wikipedia as secondary/background source

## Media
- Pexels API
- Wikimedia Commons fallback

## Semantic media ranking
- OpenCLIP

## Voice
Primary:
- OpenAI TTS
- default model: gpt-4o-mini-tts
- preferred voice: marin

Fallback:
- macOS system TTS

## Caption alignment
- WhisperX

## Computer vision
- OpenCV
- MediaPipe

## Rendering
- FFmpeg
- FFprobe

## Secrets
- OS-native secure storage
- Python keyring abstraction
- macOS Keychain on macOS
- Windows Credential Locker on Windows

## Storage
- Local project storage
- Local cache

Do NOT introduce cloud storage as a mandatory dependency.

---

# 5. CRITICAL NEW REQUIREMENT: IN-APP SETUP

One of the most important changes is that configuration currently done outside the app should be moved into the app wherever reasonable.

The user should not need to manually edit `.env` files for normal use.

Create a complete in-app setup system.

---

# 6. SETTINGS → INTEGRATIONS

Add a settings area:

> Settings → Integrations

At minimum support:

- OpenAI
- Pexels
- Brave Search
- Wikimedia
- FFmpeg
- WhisperX
- OpenCLIP

Each integration should display a status such as:

- Connected
- Not configured
- Invalid key
- Rate limited
- Quota exceeded
- Offline
- Installed
- Not installed

Example UI:

```text
OpenAI

Used for:
✓ Script generation
✓ Research planning
✓ AI editing
✓ Voice generation

API Key
••••••••••••••••••••••••4hK2

[ Change Key ]
[ Test Connection ]
[ Open Provider Dashboard ]

Status: Connected
```

Do not reveal the entire stored secret back to the frontend.

Only return something like:

```json
{
  "provider": "openai",
  "configured": true,
  "last_four": "4hK2"
}
```

---

# 7. SECURE API KEY STORAGE

Do NOT store API keys in SQLite as plaintext.

Do NOT permanently expose secrets to the frontend.

Use an abstraction such as:

```python
SecretStore
```

Backed by:

```python
keyring
```

Example behavior:

```python
keyring.set_password(
    "ClipForge",
    "OPENAI_API_KEY",
    api_key
)
```

and retrieval:

```python
keyring.get_password(
    "ClipForge",
    "OPENAI_API_KEY"
)
```

Recommended stored secret names:

- OPENAI_API_KEY
- PEXELS_API_KEY
- BRAVE_SEARCH_API_KEY

SQLite may store only non-secret metadata such as:

```text
openai_enabled = true
```

---

# 8. IMPORT EXISTING .ENV KEYS

At first startup, detect existing keys from the old environment configuration.

If found, offer:

```text
Existing API keys found

OpenAI ✓
Pexels ✓
Brave ✓

[ Import securely ]
```

When imported:

1. Read the old `.env`
2. Validate keys
3. Move/store them securely in OS Keychain / keyring
4. Mark integrations as configured
5. Stop depending on `.env` for normal use

Do not delete the user's `.env` automatically unless explicitly requested.

---

# 9. INTEGRATION API

Implement backend endpoints similar to:

```text
GET    /settings/integrations

POST   /settings/integrations/openai
POST   /settings/integrations/pexels
POST   /settings/integrations/brave

POST   /settings/integrations/openai/test
POST   /settings/integrations/pexels/test
POST   /settings/integrations/brave/test

DELETE /settings/integrations/openai
DELETE /settings/integrations/pexels
DELETE /settings/integrations/brave
```

Saving a key should behave like:

```text
User enters key
↓
Backend receives key
↓
Provider validation request
↓
Valid?
 ├─ No → return useful error
 └─ Yes
      ↓
   Save in Keychain
      ↓
   return Connected
```

---

# 10. FIRST-RUN ONBOARDING WIZARD

Add a first-run setup wizard.

Example:

```text
Welcome to ClipForge

Let's set up the services needed to create videos.

Step 1/3
OpenAI

Used for:
• Research planning
• Scripts
• Editing
• Voiceover

[ Get API Key ]
[ Paste API Key ]

sk-____________________

[ Test & Save ]
```

Then:

- Step 2: Pexels
- Step 3: Brave Search

The app can open the provider's official API dashboard in the browser.

The app should NOT try to automatically create external accounts or create provider API keys on behalf of the user.

The app SHOULD automate everything after the user pastes the key.

---

# 11. SETTINGS → SYSTEM

Add a system section.

Example:

```text
FFmpeg
Status: Ready
Version: ...
[ Verify ]

WhisperX
Status: Installed
Model: ...
[ Install / Update ]

OpenCLIP
Status: Installed
Model: ViT-B-32
[ Install / Update ]

Media Storage
/Users/.../ClipForge/data
[ Change ]

Cache
8.4 GB
[ Clear Cache ]
```

Long-term goal:
- bundle FFmpeg where practical
- install/manage local AI dependencies through the app
- minimize terminal usage

---

# 12. MAIN CREATE SCREEN

Keep the main UI simple.

Example:

```text
CLIPFORGE

What should the video be about?

┌────────────────────────────────────────────┐
│ Why does airplane food taste different?   │
│                                            │
└────────────────────────────────────────────┘

Language
English ▼

Style
Educational / Fast / Clean ▼

Platform
TikTok + Reels + Shorts ▼

[ Create Video ]
```

Optional advanced settings may exist, but should not be required.

---

# 13. INPUT TYPES

The prompt box should support:

## Topic
```text
Black holes
```

## Question
```text
Why can't we tickle ourselves?
```

## Full instruction
```text
Make a fast video explaining why airplane windows are round.
Start with a surprising hook and use real airplane footage.
```

The AI director should normalize this into structured intent.

Example:

```json
{
  "topic": "airplane windows",
  "goal": "explanation",
  "language": "en",
  "audience": "general",
  "style": "fast educational",
  "requires_research": true,
  "requested_elements": [
    "airplane footage"
  ]
}
```

---

# 14. RESEARCH PLANNING

Before searching the web, generate research questions.

Example:

```text
Why are airplane windows round?
Why did early aircraft use square windows?
What is stress concentration?
How did aircraft window design change historically?
```

Do not search blindly using the raw user prompt only.

---

# 15. RESEARCH SOURCES

Primary:
- Brave Search

Secondary:
- Wikipedia

Brave should be used for:
- authoritative sources
- recent information
- engineering/science references
- general web research

Wikipedia should be used mainly for:
- background
- entity context
- broad factual orientation

Store research as structured claims, not just raw search snippets.

Example:

```json
{
  "claim": "Sharp corners concentrate mechanical stress.",
  "confidence": 0.96,
  "sources": [...]
}
```

---

# 16. FACT VALIDATION

The script must be grounded in the research.

Classify claims as:

- SUPPORTED
- UNCERTAIN
- CONFLICTING
- UNSUPPORTED

Rules:

- SUPPORTED → may be used
- UNCERTAIN → use cautiously or research further
- CONFLICTING → resolve before use
- UNSUPPORTED → do not use

Do not allow the model to invent facts to make the video more dramatic.

---

# 17. HOOK GENERATION

Generate several candidate hooks.

Example:

```text
1. "There's a deadly reason airplane windows aren't square."
2. "These tiny curves solved a serious aviation problem."
3. "Airplane windows used to look very different."
```

Score candidates by:

- curiosity
- clarity
- truthfulness
- short-form suitability
- connection to the actual explanation

Avoid misleading clickbait.

---

# 18. SCRIPT STRUCTURE

The script output should be structured.

Example:

```json
{
  "duration": 31.4,
  "scenes": [
    {
      "id": "scene_1",
      "start": 0,
      "end": 3.1,
      "voice": "...",
      "visual": "...",
      "caption_emphasis": ["..."],
      "media_queries": ["..."]
    }
  ]
}
```

Each scene should contain:

- narration
- visual concept
- timing
- key words
- media search queries
- overlay suggestions if useful

---

# 19. STORYBOARD

Example storyboard:

| Time | Voice | Visual |
|---|---|---|
| 0–2.5s | Hook | airplane window close-up |
| 2.5–6s | history | old aircraft |
| 6–11s | problem | square window |
| 11–17s | stress | material / structure |
| 17–23s | solution | rounded window |
| 23–29s | explanation | modern aircraft |
| 29–32s | ending | wing / clouds |

The storyboard becomes the basis for media retrieval.

---

# 20. MEDIA QUERY GENERATION

Do not use overly broad queries.

Bad:

```text
airplane
```

Better:

```text
airplane window close up
passenger looking through airplane window
commercial aircraft interior window
airplane flying through clouds
```

Generate several queries per scene.

---

# 21. PEXELS INTEGRATION

Pexels is the primary stock media provider.

Use the current video API path:

```text
/v1/videos/...
```

Prefer:

1. portrait
2. square
3. landscape

because the final output is 9:16.

For every scene:

```text
multiple search queries
↓
Pexels
↓
candidate results
↓
prefilter
↓
semantic ranking
↓
selected asset
```

Store:

- provider
- media ID
- creator
- source URL
- dimensions
- duration
- orientation
- attribution information

---

# 22. WIKIMEDIA COMMONS FALLBACK

Use Wikimedia Commons when stock video is insufficient.

Typical cases:

- historical people
- historical events
- scientific diagrams
- old machines
- specific landmarks
- archival material

For every Wikimedia asset, store:

- source
- creator
- license
- attribution
- original URL

---

# 23. MEDIA RANKING WITH OPENCLIP

Do NOT select the first media result.

Use OpenCLIP to compare:

- scene description text embedding
- image / frame embeddings

For images:
- compare image directly

For videos:
- extract representative frames, for example at:
  - 20%
  - 50%
  - 80%

Score each frame.
Average or combine scores.

Final asset score should also consider:

- semantic similarity
- orientation
- resolution
- duration
- motion
- duplicate penalty
- previous scene similarity

---

# 24. MEDIA DIVERSITY RULES

Avoid repetitive output.

Rules:

- identical asset should generally not be reused
- same source should not appear in too many consecutive scenes
- avoid 5 nearly identical airplane shots in a row
- alternate between:
  - video
  - still image
  - close-up
  - wide shot
  - detail
  - different angles

---

# 25. LOCAL ASSET CACHE

Project layout example:

```text
data/
  projects/
    <project_id>/
      assets/
        videos/
        images/
      audio/
      render/
      metadata/
```

Global cache:

```text
cache/
```

Use:
- provider ID
- URL
- SHA-256
- metadata

to avoid unnecessary repeated downloads.

---

# 26. SMART CROP

Do not blindly center-crop landscape video.

Use:

- MediaPipe
- OpenCV

Detect:
- faces
- people
- relevant subject region

Then determine crop center for 9:16 output.

Fallback:

```text
no meaningful subject detected
→ center crop
```

---

# 27. IMAGE MOTION

Static images should receive subtle motion when appropriate.

Examples:

- slow zoom 100% → 108%
- subtle pan left → right
- slow subject-focused zoom
- Ken Burns style movement

Avoid exaggerated slideshow effects.

---

# 28. VOICE SYSTEM

Primary voice provider:

- OpenAI TTS

Default:
- model: gpt-4o-mini-tts
- voice: marin

Fallback:
- macOS system voice

Behavior:

```text
OpenAI
↓ failure
retry
↓ failure
macOS TTS
```

The fallback should not silently replace OpenAI unless necessary.

---

# 29. VOICE SETTINGS UI

Add:

> Settings → Voice

Example:

```text
Provider
OpenAI

Voice
Marin ▼

Style
Energetic Shortform ▼

Speed
1.05x

Instructions
"Speak naturally, confidently and quickly.
Use short pauses.
Modern short-form documentary style."

[ Preview Voice ]
```

---

# 30. WORD-LEVEL TIMING

Use WhisperX after voice generation.

Target output:

```json
[
  {
    "word": "Airplane",
    "start": 0.12,
    "end": 0.48
  },
  {
    "word": "windows",
    "start": 0.49,
    "end": 0.82
  }
]
```

This drives dynamic captions.

---

# 31. CAPTION DESIGN

Avoid showing full paragraphs.

Use short groups such as:

```text
THERE'S A REASON

AIRPLANE WINDOWS

AREN'T SQUARE
```

Typical chunk size:
- approximately 2–5 words

Allow emphasized key words.

Example:

```text
Airplane windows weren't always ROUND
```

`ROUND` may be emphasized visually.

Keep captions within safe areas for TikTok/Reels/Shorts UI.

---

# 32. VISUAL PACING

Do not change visuals only at sentence boundaries.

Recommended typical visual changes:

```text
every 1.5–4 seconds
```

Faster during:
- hook
- high-energy intro

Slightly slower during:
- explanation
- complex concepts

A single narration sentence may use multiple visual assets.

---

# 33. LIGHTWEIGHT OVERLAYS

Allow small informational overlays.

Examples:

```text
≈ 1950s
```

```text
PRESSURE ↑
```

```text
Stress concentration
```

Do not overload the screen.

---

# 34. MUSIC AND SOUND EFFECTS

Do not require another paid API at first.

Use a local library:

```text
music/
  documentary/
  science/
  dark/
  upbeat/

sfx/
  whoosh/
  hit/
  click/
  rise/
```

AI chooses suitable assets.

Voice must remain clearly audible.

Use loudness normalization.

---

# 35. INTERNAL TIMELINE FORMAT

Do not let the LLM directly generate raw FFmpeg commands as the main architecture.

Use an intermediate timeline format.

Example:

```json
{
  "resolution": [1080, 1920],
  "fps": 30,
  "tracks": {
    "video": [],
    "voice": [],
    "music": [],
    "captions": [],
    "overlays": []
  }
}
```

Architecture:

```text
AI Director
↓
Storyboard
↓
Timeline JSON
↓
Renderer
↓
FFmpeg
```

This separation is critical.

---

# 36. RENDERER

Renderer should interpret the timeline and build FFmpeg operations.

Responsibilities:

- resize
- crop
- trim
- concatenate
- image motion
- video speed if needed
- audio mix
- captions
- overlays
- transitions
- loudness normalization
- export

Do not combine research logic and rendering logic in the same service.

---

# 37. TECHNICAL QUALITY CONTROL

After rendering, automatically verify:

- output file exists
- video stream exists
- audio stream exists
- resolution is correct
- duration is plausible
- no major black-frame sections
- no missing media
- no caption overflow
- no silent unexpected sections
- no obvious audio clipping
- expected timeline duration matches render duration

Use:

- FFprobe
- OpenCV
- audio analysis
- internal timeline metadata

---

# 38. OPTIONAL FUTURE AI VIDEO QC

Optional later feature:

Gemini can inspect the final video and assess:

- visual relevance
- scene/narration match
- pacing
- obvious media mismatch

Do NOT make this mandatory for the first stable implementation.

---

# 39. PROJECT VIEW

After rendering, show something like:

```text
┌───────────────────────┐
│                       │
│       VIDEO           │
│       PREVIEW         │
│                       │
└───────────────────────┘

32.4 sec

[ Play ]

[ Edit with AI ]
[ Edit Timeline ]
[ Export ]

Sources
Assets
Script
History
```

---

# 40. NATURAL-LANGUAGE EDITING

This is a core feature.

The user may write:

```text
Make the intro faster.
```

```text
Replace the first two clips.
```

```text
Use a close-up of the aircraft body in scene 4.
```

```text
Make the whole video shorter.
```

```text
Make the voice more energetic.
```

Do NOT regenerate the entire project unless necessary.

---

# 41. EDIT DIRECTOR

The edit model should receive:

- user request
- current project
- script
- storyboard
- timeline
- assets
- relevant previous revision

It should output operations.

Example:

```json
{
  "operations": [
    {
      "type": "replace_asset",
      "scene_id": "scene_2"
    },
    {
      "type": "shorten_scene",
      "scene_id": "scene_1",
      "amount": 0.7
    }
  ]
}
```

Only affected parts should be regenerated.

---

# 42. REVISION SYSTEM

Store project history.

Example:

```text
Revision 1
Original generation

Revision 2
Make hook faster

Revision 3
Replace clip 4

Revision 4
Make captions larger
```

Support:

- Undo
- Redo
- Restore version

---

# 43. DATABASE MODEL

SQLite is sufficient.

Recommended logical tables:

- projects
- revisions
- scenes
- claims
- sources
- assets
- asset_scores
- voice_tracks
- captions
- timelines
- renders
- jobs
- settings

Important:

API keys must NOT be stored in these tables.

---

# 44. JOB SYSTEM

The generation pipeline should use explicit states.

Example:

```text
CREATED
↓
RESEARCHING
↓
SCRIPTING
↓
PLANNING
↓
SEARCHING_ASSETS
↓
DOWNLOADING
↓
VOICE
↓
ALIGNING
↓
RENDERING
↓
QC
↓
COMPLETED
```

The frontend should display progress.

Example:

```text
Researching topic... ✓
Writing script... ✓
Finding media... ✓
Generating voice... ✓
Creating captions... 73%
Rendering video...
```

If a job fails, resume from the nearest safe stage instead of restarting everything.

---

# 45. PROVIDER ABSTRACTION

Do not scatter provider-specific code across the codebase.

Recommended architecture:

```text
providers/
  llm/
    openai.py

  search/
    brave.py

  stock/
    pexels.py
    wikimedia.py

  tts/
    openai.py
    macos.py
```

Use interfaces such as:

```python
tts.generate(...)
search.search(...)
media.search(...)
```

rather than direct SDK calls everywhere.

This allows future replacement or addition of providers.

---

# 46. RECOMMENDED BACKEND STRUCTURE

Target organization:

```text
clipforge/

  api/
    projects.py
    settings.py
    integrations.py
    render.py

  providers/
    openai/
    brave/
    pexels/
    wikimedia/

  services/
    director.py
    research.py
    fact_checker.py
    script_writer.py
    storyboard.py
    media_search.py
    media_ranker.py
    downloader.py
    smart_crop.py
    voice.py
    alignment.py
    captions.py
    timeline.py
    renderer.py
    quality_control.py
    editor.py

  models/
    project.py
    scene.py
    asset.py
    source.py
    revision.py

  security/
    secrets.py

  jobs/
    pipeline.py

  storage/
    projects.py
    cache.py
```

Do not force a rewrite if the current repository differs.
Refactor incrementally.

---

# 47. LOCAL MODEL MANAGER

Add:

> Settings → Local AI

Example:

```text
OpenCLIP
Semantic media ranking
Status: Not installed
Size: ...
[ Install ]

WhisperX
Word-level caption timing
Status: Installed
[ Update ]
```

Installation should be handled through backend jobs.

Flow:

```text
UI
↓
FastAPI job
↓
Download
↓
Verify
↓
Install
↓
Ready
```

The long-term objective is to make ClipForge usable without terminal configuration.

---

# 48. FAILURE HANDLING

Integrations should expose meaningful statuses.

Examples:

```text
Connected
Invalid key
Quota exceeded
Rate limited
Offline
Not installed
```

Fallback examples:

```text
Pexels
↓
Wikimedia
↓
local media
```

```text
OpenAI TTS
↓
retry
↓
macOS TTS
```

Research should NOT silently invent information if Brave fails.

Instead:
- use other available sources
- flag insufficient evidence
- stop generation if the required facts cannot be verified safely

---

# 49. SOURCE AND CREDIT MANAGEMENT

Every asset should retain provenance.

For Pexels:

```json
{
  "provider": "pexels",
  "creator": "...",
  "source": "...",
  "license": "pexels"
}
```

For Wikimedia:

```json
{
  "provider": "wikimedia",
  "creator": "...",
  "source": "...",
  "license": "...",
  "attribution": "..."
}
```

Add a project view:

> Sources & Credits

Include:
- research sources
- media sources
- creators
- licenses
- attribution

---

# 50. THINGS TO AVOID FOR NOW

Do not overcomplicate the product with:

- mandatory cloud storage
- user account system
- Kubernetes
- large distributed infrastructure
- five LLM providers
- mandatory ElevenLabs
- mandatory generative video models
- unnecessary paid APIs

The core product should first become reliable using:

- OpenAI
- Brave
- Pexels
- Wikimedia
- local processing
- FFmpeg

Generative video can later become an additional optional media provider.

---

# 51. OPTIONAL FUTURE AI VIDEO PROVIDER

Possible future architecture:

```text
Media Sources

Pexels
Wikimedia
Local Library
AI Generated
```

LTX or another generative video system may later be plugged into this layer.

Do not make the main pipeline dependent on it.

---

# 52. CURRENT PRIORITY ORDER

Implement in this order unless repository constraints require a small adjustment.

## Phase 1 — Stabilize existing project
1. Inspect current repository
2. Identify current working features
3. Identify failing tests/errors
4. Fix known backend issues
5. Preserve working behavior

Important known area:
- investigate current edit-project/settings related backend errors if still present

---

## Phase 2 — Build the full integration/settings system

Build:

- Settings page
- Integrations page
- OpenAI key field
- Pexels key field
- Brave key field
- validation
- test buttons
- secure storage
- provider status
- `.env` import
- first-run onboarding

This is the highest-priority feature.

---

## Phase 3 — Real Pexels media pipeline

Implement:

- image search
- video search
- modern Pexels video API
- download
- metadata
- attribution
- cache
- orientation preference

Replace current visual placeholders with real media.

---

## Phase 4 — Storyboard-to-media connection

Each scene should automatically generate:
- visual description
- multiple queries
- candidate media
- chosen media

---

## Phase 5 — OpenCLIP ranking

Add:
- local model management
- image scoring
- video frame scoring
- semantic ranking
- diversity penalties

---

## Phase 6 — Wikimedia fallback

Add:
- media search
- download
- licensing metadata
- attribution
- fallback logic

---

## Phase 7 — Smart Crop

Add:
- person/face detection
- subject-aware crop
- 9:16 crop
- center crop fallback

---

## Phase 8 — Finalize voice system

Add:
- OpenAI TTS default
- marin default voice
- voice settings UI
- voice preview
- macOS fallback

---

## Phase 9 — WhisperX

Add:
- word-level timestamps
- installation status
- model management
- alignment service

---

## Phase 10 — Captions

Add:
- word chunking
- emphasis
- timing
- safe areas
- style system

---

## Phase 11 — Timeline architecture

Create a stable intermediate timeline model.

Separate:
- planning
- timeline
- rendering

---

## Phase 12 — Music and SFX

Use local assets first.

Add:
- categories
- volume mixing
- selection
- normalization

---

## Phase 13 — Full end-to-end generation

A single button must successfully produce:

```text
Prompt
→ research
→ script
→ storyboard
→ media
→ voice
→ captions
→ timeline
→ render
→ QC
→ MP4
```

---

## Phase 14 — Jobs and progress

Add:
- resumable pipeline
- stage tracking
- frontend progress
- meaningful failure messages

---

## Phase 15 — AI editing

Implement:
- natural-language editing
- structured edit operations
- partial regeneration

---

## Phase 16 — Revision system

Implement:
- undo
- redo
- restore
- history

---

## Phase 17 — Technical QC

Add automated post-render checks.

---

## Phase 18 — Remove remaining terminal dependencies

Move installation and configuration into the UI where practical.

---

# 53. ACCEPTANCE CRITERIA FOR FIRST STRONG VERSION

A build should not be considered "complete" until the following is possible:

1. Launch ClipForge locally.
2. Open Settings.
3. Paste OpenAI, Pexels, and Brave API keys in the app.
4. Test each connection.
5. Restart the app.
6. Keys remain securely configured.
7. Enter a topic in the main prompt.
8. ClipForge researches the topic.
9. ClipForge creates a fact-grounded script.
10. ClipForge determines a suitable duration automatically.
11. ClipForge creates a storyboard.
12. ClipForge searches for real media.
13. ClipForge selects meaningful media.
14. ClipForge creates OpenAI voiceover.
15. ClipForge generates timed captions.
16. ClipForge renders a 9:16 video.
17. ClipForge produces a playable MP4.
18. User can type an edit request.
19. ClipForge modifies only the necessary project parts.
20. User can restore an earlier revision.

---

# 54. SECURITY REQUIREMENTS

Mandatory:

- Never log API keys.
- Never return full secrets through API responses.
- Never store secrets in SQLite plaintext.
- Never commit secrets to source control.
- Never expose API keys in frontend bundles.
- Validate provider keys before saving where practical.
- Mask keys in UI.
- Use OS-native secure storage.
- Sanitize logs.

---

# 55. ENGINEERING RULES

Follow these rules while implementing:

1. Read the existing code before changing architecture.
2. Reuse working functionality.
3. Keep functions and services reasonably small.
4. Avoid giant multi-purpose renderer/services.
5. Add typed models where useful.
6. Validate all external provider responses.
7. Handle timeouts and rate limits.
8. Use retries carefully.
9. Cache expensive results.
10. Avoid duplicate downloads.
11. Preserve project reproducibility where possible.
12. Write tests for important service boundaries.
13. Do not mix secret storage with regular settings storage.
14. Do not mix AI planning logic with FFmpeg implementation.
15. Keep providers replaceable.
16. Use structured JSON outputs for AI orchestration.
17. Validate AI JSON before using it.
18. Fail explicitly instead of silently creating broken videos.
19. Keep UI feedback clear.
20. Do not require technical knowledge from the user for normal operation.

---

# 56. EXPECTED WORKING STYLE FOR GEMINI

When continuing this project:

- inspect the repository first
- understand the existing architecture
- do not assume files or APIs exist
- make changes incrementally
- run relevant tests after each major step
- repair failures before continuing
- preserve current functionality
- summarize what changed after each phase
- state any migration that was necessary
- do not ask the user to manually perform tasks that can reasonably be automated in the app
- only request external action when unavoidable, such as creating a provider account or obtaining an API key

When implementing a large phase, break it into smaller internally coherent steps.

Do not attempt to rewrite the entire application in one uncontrolled pass.

---

# 57. FIRST TASK TO EXECUTE

Start with the following:

## Step 1
Inspect the entire current ClipForge repository.

Determine:

- frontend structure
- backend structure
- existing API routes
- current database schema
- current project model
- current `.env` handling
- existing OpenAI integration
- existing Brave integration
- existing Pexels integration
- existing renderer
- current media placeholder behavior
- current voice implementation
- current edit/revision logic
- failing tests
- currently broken functionality

## Step 2
Produce a concise implementation assessment:

```text
Already working:
- ...

Partially working:
- ...

Broken:
- ...

Missing:
- ...

Architecture risks:
- ...
```

## Step 3
Then begin implementing the **Settings / Integrations / Secure Secret Storage** phase.

Do not jump ahead to WhisperX, Smart Crop, or OpenCLIP until the integration system is functional and tested.

---

# 58. DEFINITION OF THE NEXT MILESTONE

The next milestone is complete when:

- ClipForge has an Integrations settings screen
- OpenAI key can be entered in-app
- Brave key can be entered in-app
- Pexels key can be entered in-app
- keys can be tested
- invalid keys show useful errors
- valid keys are securely stored
- saved secrets survive restart
- frontend never receives full stored keys
- `.env` import is available
- providers read keys from the new SecretStore
- existing generation features still work
- tests pass

Only after this milestone should development move to the full real-media pipeline.

---

# 59. FINAL PRODUCT EXPERIENCE

The desired final experience should feel like this:

```text
Open ClipForge
↓
Enter:
"Why do cats purr?"
↓
Create Video
↓
ClipForge researches
↓
ClipForge writes a grounded script
↓
ClipForge decides optimal duration
↓
ClipForge creates storyboard
↓
ClipForge searches Pexels and Wikimedia
↓
OpenCLIP ranks media
↓
ClipForge downloads and crops media
↓
OpenAI creates voiceover
↓
WhisperX aligns words
↓
ClipForge generates captions
↓
Music and SFX are added
↓
FFmpeg renders
↓
Quality control runs
↓
Video is ready
```

Then the user can type:

```text
The intro is too slow.
Make the first five seconds faster and use more dramatic cat footage.
```

ClipForge should then:

```text
interpret edit
↓
change affected scenes
↓
search new media if required
↓
regenerate only required audio/captions
↓
update timeline
↓
render new revision
↓
preserve previous revision
```

That is the target system.

---

# END OF MASTER SPECIFICATION

Treat this document as authoritative unless the user explicitly changes a requirement.
