# Multi-platform publishing V1 (YouTube + Instagram + TikTok)

## Architecture

| Concern | Authority |
| --- | --- |
| Connected accounts (any number per platform) | `publishing_accounts` (`clipforge/publishing/accounts.py`), unique `(platform, external_account_id)` |
| Credentials | OS keyring only, one entry per account: `YOUTUBE_REFRESH_TOKEN:<id>`, `TIKTOK_REFRESH_TOKEN:<id>`, `INSTAGRAM_TOKEN:<id>` |
| Developer apps | keyring (`YOUTUBE_OAUTH_CLIENT_*`, `TIKTOK_CLIENT_KEY/SECRET`, `META_APP_ID/SECRET`) or environment; API answers only "configured" |
| Non-secret platform settings | `publishing_platform_configs` (TikTok "app audited", Meta Login for Business `config_id`) |
| YouTube publications | `youtube_uploads` (unchanged authority; bound to an account through `channel_id`) |
| Instagram/TikTok publications + ClipForge-owned schedules | `social_publications` (`clipforge/publishing/publications.py`) |
| Scheduler | `clipforge/publishing/scheduler.py` (DB-driven loop with leases; stateless between ticks) |
| Unified read model (Videos, Queue, sheet) | `clipforge/publishing/read_model.py` + `youtube/library.py` |
| Capabilities (UI renders only these) | `clipforge/publishing/capabilities.py` |

All platforms upload the same canonical final master (`exporter.resolve_final_master`).

### YouTube migration

The old single connection (`youtube_connections.slot = "primary"` + global
`YOUTUBE_REFRESH_TOKEN`) is adopted as a `publishing_accounts` row by Alembic
`0013_multiplatform_publishing` and, for databases prepared by the app's
startup path, lazily by `accounts.migrate_legacy_youtube`. Nothing is deleted:
the legacy row stays, uploads/schedules/analytics keep their `channel_id`, and
on first use the global token is copied (and read back) into the account's own
keyring entry. The legacy key is removed only when that account is reconnected
or disconnected. Only the adopted "primary" account may ever read it.

### Scheduling and offline behaviour

* YouTube keeps native scheduling (`publishAt`).
* Instagram and TikTok have no future-publish timestamp: ClipForge stores the
  publication (`state=scheduled`) bound to project, revision, render revision,
  final-master SHA-256, account, metadata snapshot, time and time zone, and
  the backend publishes it when due - **only while the ClipForge backend runs
  with internet access**.
* Before uploading, the bound final master is resolved and re-hashed; a
  missing or changed file fails closed (`render_missing` / `render_changed`).
* A post whose due time passed more than `PUBLISHING_MISSED_GRACE_MINUTES`
  (default 15) ago while ClipForge was not running becomes **missed** and is
  never published late on its own (Publish now / Reschedule / Cancel).
* An upload interrupted by a restart restarts from scratch (no partial post
  exists); a video whose bytes fully reached the provider is never uploaded
  again - its status is polled instead.
* Transient errors (timeouts, 5xx, rate limits) retry with backoff 1, 2, 4,
  8 … min (max `PUBLISHING_MAX_ATTEMPTS`, default 5); auth, permission,
  policy and media errors fail immediately.

## External setup

### TikTok (Content Posting API, Direct Post)

1. Create an app at developers.tiktok.com; add **Login Kit** and the
   **Content Posting API** product with **Direct Post** enabled.
2. Redirect URI. TikTok's Web platform requires an HTTPS, non-localhost
   redirect; its **Desktop** platform (Login Kit for Desktop) accepts
   `localhost`/`127.0.0.1` with a port and requires PKCE (ClipForge always
   sends a hex SHA-256 code challenge). Register exactly
   `http://localhost:8000/api/publishing/tiktok/oauth/callback` on the Desktop
   platform (or set `TIKTOK_OAUTH_REDIRECT_URI` to what you registered). If
   the portal does not accept the Content Posting API for a Desktop app, an
   HTTPS tunnel to the local backend is needed instead - ClipForge does not
   ship one (see the report's limitations).
3. Scopes: `user.info.basic`, `video.publish` (approve them for the app).
4. Paste the client key and secret in Settings → Integrations → TikTok.
5. Until the app passes TikTok's Content Posting **audit**, TikTok only allows
   private (`SELF_ONLY`) posts, to accounts set to private, from a capped
   number of users. Tick "This TikTok app passed TikTok's audit" only after
   TikTok approved it - ClipForge never downgrades a public request.

### Instagram (Instagram API with Facebook Login for Business)

Why not "Instagram API with Instagram Login" (Business Login for Instagram)?
It would be the simpler sign-in (instagram.com OAuth, `graph.instagram.com`,
scopes `instagram_business_basic` + `instagram_business_content_publish`, no
Facebook Page, long-lived tokens refreshable via
`graph.instagram.com/refresh_access_token`). But Meta's content-publishing
guide limits resumable upload (`upload_type=resumable` + bytes to
`rupload.facebook.com`) to apps that implement **Facebook Login for
Business**; with Instagram Login a Reel can only be created from a publicly
reachable `video_url`. ClipForge uploads the local final master and does not
host videos publicly, so Instagram Login cannot publish ClipForge videos
without new hosting infrastructure. Re-checked 2026-10-06; Meta's own pages
were blocked from the build environment, so this rests on Meta's quoted
documentation text in independent sources.

1. Create a Meta app (Business type) at developers.facebook.com; add
   **Facebook Login for Business** and the **Instagram** (Graph API) product.
2. Valid OAuth redirect URI:
   `http://localhost:8000/api/publishing/instagram/oauth/callback`
   (or set `INSTAGRAM_OAUTH_REDIRECT_URI`). Meta allows `localhost` redirects
   while the app is in development mode; a live app may require HTTPS.
3. Permissions: `instagram_basic`, `instagram_content_publish`,
   `pages_show_list`, `pages_read_engagement` (optionally create a Login for
   Business configuration with them and paste its configuration ID).
4. The Instagram account must be a **professional** (Business or Creator)
   account linked to a Facebook Page the signing-in user manages.
5. In development mode only people with a role on the app (admins,
   developers, testers) can connect; publishing for others needs App Review
   (Advanced Access) and Business Verification.
6. User tokens last about 60 days; reconnect when Settings shows the warning.
7. Meta limits API publishing to 100 posts per 24 hours per account.
8. `META_GRAPH_VERSION` (default `v25.0`) selects the Graph API version.

### YouTube

Unchanged: Google OAuth client with the redirect
`http://localhost:8000/api/youtube/oauth/callback`. Add more channels with
"Add account" and pick another Google account or brand channel in Google's
account chooser.

## Troubleshooting a failed Instagram/TikTok post

Every failed or retried attempt keeps secret-free provider diagnostics in the
publication's event log and in the API (`GET /api/publishing/publications/<id>`
→ `publication.error.diagnostics`):

* `code` - ClipForge's classification (e.g. `invalid_request`)
* `provider_code` - TikTok's/Meta's own code (e.g. `invalid_param`)
* `provider_message` - the provider's human-readable reason (URLs, tokens and
  credential values removed, max 300 characters)
* `log_id` - TikTok's request id; quote it when contacting TikTok support
* `http_status`, `context` (which step failed, e.g. `post initialization`)

The same fields are logged once per failure by
`clipforge.publishing.publications` (never the raw response body). The UI shows
the provider message and a copyable "Provider code … · HTTP … · log_id …" line.
