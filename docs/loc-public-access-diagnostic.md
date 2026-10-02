# LOC public-access diagnostic (2026-10-02)

## Official contract

Rechecked [LOC JSON/YAML access documentation](https://www.loc.gov/apis/json-and-yaml/):
no API key or authentication is required. The [official endpoints](https://www.loc.gov/apis/json-and-yaml/requests/endpoints/)
document `/search/?q=...&fo=json`, the `results` array, `fa` facets and `c`/`sp`
pagination. [Working within limits](https://www.loc.gov/apis/json-and-yaml/working-within-limits/)
documents 20 JSON/YAML requests/minute and a one-hour block on exceeding the
limit; CAPTCHA challenges can also occur below that rate. Neither a 403 nor an
HTML response alone proves which blocking mechanism occurred.

The adapter uses `https://www.loc.gov/search/`, `fo=json`, `c=12`, `sp=1`,
`fa=online-format:image`, and httpx parameter encoding. Item enrichment uses
the official `/item/` URL and `fo=json&at=item,resources`. No key, Authorization
or X-Api-Key header is configured for LOC. Timeouts remain 4 seconds to connect
and 10 seconds for reads/writes/pool acquisition. JSON API redirects remain
explicitly unfollowed: an unexpected redirect is a bounded provider failure,
with its sanitized destination recorded, rather than unbudgeted requests to an
arbitrary host. The canonical endpoint already has its trailing slash.

## Persisted real failure

Project `484ed264-7ed8-4f84-bd58-2f5dbb9eb935`, question
“Warum wurde die Berliner Mauer gebaut?”, retains
`diagnostics/visual-acquisition.json`. Two scene searches record:

| Scene | Query | Requests | Results | Old category |
| --- | --- | --- | --- | --- |
| scene_01_01 | berlin august barbed wire street archival | 1 | 0 | invalid_credentials |
| scene_02_01 | baute mauer weil immer menschen | 1 | 0 | invalid_credentials |

Both routed LOC first for `historical_or_archival`. Wikimedia subsequently
ran, Openverse recorded its own independent quota exhaustion, and Pexels
widening continued. Europeana was not enabled in these saved routes.

The original diagnostics did **not** store HTTP status, headers, body, redirect
history or wire User-Agent. There is no retained HTTP log for this project.
The shared adapter sets `disabled` on 401/403, then the generic external
boundary labels either status `invalid_credentials`. That explains the
classification but does not establish **which** of 401/403 actually occurred,
or whether rate limiting, CAPTCHA or another denial caused it.

Reconstructed first request (from saved query and adapter parameters, not a
captured wire log):

```text
https://www.loc.gov/search/?q=berlin+august+barbed+wire+street+archival&fo=json&c=12&sp=1&fa=online-format%3Aimage
```

Before this fix the adapter supplied only `Accept: application/json`; the
installed httpx 0.28.1 defaults to `python-httpx/0.28.1`. The historical wire
value is not recoverable. New LOC JSON requests identify themselves as
`ClipForge (public LOC JSON client)`; this is identification, not a claim that
changing User-Agent bypasses a provider block.

## Bounded real Mac request

One read-only metadata request used the reconstructed URL, existing Accept
header, 4/10-second timeouts, no redirects or credentials, and a 256-KiB body
ceiling. No asset downloads were attempted. It failed before HTTP:

```text
httpx.ConnectError: [Errno 8] nodename nor servname provided, or not known
```

**Environment blocked**: no HTTP status, response headers/body, result count,
first result or redirect response was received. A public API response was not
validated live in this restricted execution environment. Documentation access
through the web tool is separate from this Mac request.

## Fix and evidence

LOC 401/403 and unexpected redirects now use existing `provider_error`, not
configured-credential semantics. LOC 429 remains `rate_limited`; malformed
JSON remains `malformed_response`; the existing external boundary still
categorizes timeout/network errors. Search denials disable only that LOC
adapter instance, avoiding repeated denied requests while other routes remain
available. Download/preview denials use the same public-provider classification.
Credentialed providers retain their current classification.

Routed failure evidence now persists HTTP status, sanitized endpoint and
allowlisted public parameters, bounded User-Agent, selected response headers,
redirect count/target, body format and a bounded CAPTCHA indicator. It excludes
raw bodies, cookies, Authorization, arbitrary query parameters and credential
headers. Failed streamed downloads are not read for diagnostics. A 403 stays
`provider_error` even with Retry-After; only HTTP 429 establishes that category
in this implementation. Diagnostics do not claim a specific historical cause.

Mocked regressions exercise no-key registry enablement, URL encoding, successful
public normalization, 401/403/429/503/redirects, malformed JSON, streamed
download failures, bounded timeout/redirect configuration, and actual historical
pooling/widening to Europeana/Wikimedia/Openverse after denial, throttling,
timeout or malformed data. Routing, semantic rejection, rights, three-query
limit and acquisition budgets are unchanged.

## Real Mac retest

Validation: 27 new LOC regressions passed; 188 focused LOC/provider/router/
historical/strict-selection/fallback tests passed, including all 42 Kaugummi
mechanism regressions. Full backend: 1907 passed. Ruff and `git diff --check`
passed. Frontend unchanged.

Restart the API/worker on the updated feature branch. Generate a NEW Berlin
Wall project. Inspect LOC `failure` and `failure_evidence` in
`diagnostics/visual-acquisition.json`: a denied public request must never read
`invalid_credentials`; successful requests still require item rights and scene
acceptance. Confirm historical widening and the existing visual fallback chain
complete when LOC is unavailable. Do not add a LOC key or weaken rights.
