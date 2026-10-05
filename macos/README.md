# ClipForge macOS launcher

`ClipForge.app` starts the existing local backend and frontend, waits for the
health endpoints, then opens <http://localhost:3000>. `ClipForge Stop.app`
stops only processes whose PID and launch time were recorded by the launcher.

The app bundles intentionally reference this checkout at
`/Users/bene/Documents/ChatGPT/ClipForge`; they do not copy `.env` or secrets.
Runtime logs and PID records are stored in `.clipforge-runtime/`, which is
ignored by Git.

The launcher runs whatever is checked out in that directory, including
uncommitted changes. Every start writes the checkout's path, branch, commit and
dirty state to `.clipforge-runtime/backend.log` (`Launcher checkout: ...`),
followed by what the backend itself reports from `/api/health`
(`Backend started|already running: ...`). If an already-running backend
reports a different checkout or commit, a `WARNING` line and a notification
appear. `scripts/mac/clipforge-local.sh --status` prints both.

To make the launcher refuse to start any other checkout, write the expected
branch and/or commit (prefix allowed) into the runtime directory, or set
`CLIPFORGE_EXPECTED_BRANCH` / `CLIPFORGE_EXPECTED_COMMIT`:

```zsh
echo test > .clipforge-runtime/expected-branch
echo 200e3f2 > .clipforge-runtime/expected-commit
```

Remove those files to go back to starting whatever is checked out.

The bundles use tiny native arm64 launch stubs; all startup/stop behavior remains
in `scripts/mac/clipforge-local.sh`. `ClipForge.app` is an `LSUIElement` agent
because its visible UI is the browser; this lets the transient launcher finish
without leaving a headless foreground application in the Dock. To rebuild the
executables after changing the stubs:

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
