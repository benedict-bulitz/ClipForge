# ClipForge macOS launcher

`ClipForge.app` starts the existing local backend and frontend, waits for the
health endpoints, then opens <http://localhost:3000>. `ClipForge Stop.app`
stops only processes whose PID and launch time were recorded by the launcher.

The app bundles intentionally reference this checkout at
`/Users/bene/Documents/ChatGPT/ClipForge`; they do not copy `.env` or secrets.
Runtime logs and PID records are stored in `.clipforge-runtime/`, which is
ignored by Git.

The bundles use tiny native arm64 launch stubs; all startup/stop behavior remains
in `scripts/mac/clipforge-local.sh`. To rebuild them after changing the stubs:

```zsh
clang -O2 -Wall -Wextra -o macos/ClipForge.app/Contents/MacOS/ClipForge macos/clipforge-launcher.c
clang -O2 -Wall -Wextra -o "macos/ClipForge Stop.app/Contents/MacOS/ClipForge Stop" macos/clipforge-stop.c
chmod 755 macos/ClipForge.app/Contents/MacOS/ClipForge "macos/ClipForge Stop.app/Contents/MacOS/ClipForge Stop"
```

After reviewing the bundles, install/replace them from macOS Terminal with:

```zsh
ditto "/Users/bene/Documents/ChatGPT/ClipForge/macos/ClipForge.app" "/Applications/ClipForge.app"
ditto "/Users/bene/Documents/ChatGPT/ClipForge/macos/ClipForge Stop.app" "/Applications/ClipForge Stop.app"
open "/Applications/ClipForge.app"
```
