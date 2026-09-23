# ClipForge local music library

Add only real, licensed background-music assets to this directory. ClipForge
does not generate oscillator beds or download commercial music.

Place each audio asset under `tracks/`, for example:

```text
apps/api/music-library/
├── manifest.json
└── tracks/
    └── calm-documentary-01.mp3
```

Add a matching entry to `manifest.json`:

```json
{
  "track_id": "calm-documentary-01",
  "title": "Calm Documentary 01",
  "file": "tracks/calm-documentary-01.mp3",
  "mood": "documentary",
  "energy": "low",
  "tags": ["curious", "warm", "science"],
  "source": "Artist or library name",
  "license": "CC BY 4.0",
  "attribution": "Artist — Title, CC BY 4.0",
  "duration_seconds": 96.4
}
```

Required fields are `track_id`, `title`, `file`, `mood`, `energy`, `source`,
and `license`. `file` must be a real local `.mp3`, `.m4a`, `.ogg`, `.wav`,
`.aac`, or `.flac` file under this folder. The loader rejects missing files,
paths outside this library, and entries without license metadata.
