# Visual sources V2 contracts

Checked against official documentation/source on 2026-10-02. The adapters expose
**photos only** in this phase; audio, streaming video, PDF documents, and records
without a usable image are excluded. Existing Pexels/Pixabay videos remain active.

| Source | Contract and credentials | Implemented bounded request |
| --- | --- | --- |
| Openverse | [Official consumer entry point](https://api.openverse.org/v1/), [official client](https://docs.openverse.org/packages/js/api_client/index.html), [current request/response serializers](https://github.com/WordPress/openverse/blob/main/api/api/serializers/media_serializers.py), [image serializer](https://github.com/WordPress/openverse/blob/main/api/api/serializers/image_serializers.py). Anonymous requests supported; optional OAuth account is unnecessary here. Consumer Swagger was blocked during review, so current official source supplies schema evidence. Images and audio, no video. | `https://api.openverse.org/v1/images/`, `q`, `page=1`, `page_size=20`, CC0/PDM/BY filter. Exclude Flickr upstream and downstream. UUID, original landing/media URLs, source/provider, creator URL, dimensions, thumbnail, tags, license/version/URL and attribution retained. |
| NASA | [Official API PDF, v1.22.0](https://images.nasa.gov/docs/images.nasa.gov_api_docs.pdf), linked by [NASA API portal](https://api.nasa.gov/). Library examples use no API key; do not confuse this endpoint with api.nasa.gov key requirements. No numeric Library rate limit established in this contract. Search supports images/video/audio. | `https://images-api.nasa.gov/search`: `q`, `media_type=image`, `page=1`, `page_size=12`; Collection+JSON `items.data`, preview `links`, NASA ID/title/description/keywords/photographer/center/date. Up to three items enriched via `/metadata/{id}` and returned metadata location; eligible originals resolved with `/asset/{id}`. All metadata/manifest calls consume the shared search budget and reserve widening capacity. |
| Europeana | [Search API](https://europeana.atlassian.net/wiki/spaces/EF/pages/2385739812), [authentication](https://europeana.atlassian.net/wiki/spaces/EF/pages/2462351393), [fair use](https://europeana.atlassian.net/wiki/spaces/EF/pages/2704146433). Registered API key required; preferred `X-Api-Key` header, deprecated URL `wskey` unused. No universal numeric rate established; 429 triggers cooldown. Search supports IMAGE/VIDEO/TEXT/SOUND/3D. | `https://api.europeana.eu/record/v2/search.json`, `query`, `qf=TYPE:IMAGE`, `start=1`, `rows=12`, `profile=standard`, `media=true`, `reusability=open`. `items`: `id`, `guid`, `dataProvider`, `provider`, `dcCreator`, `title`, `dcDescription`, `year`, `rights`, `edmIsShownAt/By`, `edmPreview`. One direct image and one unambiguous rights URI required. |
| Library of Congress | [Requests](https://www.loc.gov/apis/json-and-yaml/requests/), [parameters](https://www.loc.gov/apis/json-and-yaml/requests/parameters/), [search response](https://www.loc.gov/apis/json-and-yaml/responses/search-results/), [item response](https://www.loc.gov/apis/json-and-yaml/responses/item-and-resource/), [limits](https://www.loc.gov/apis/json-and-yaml/working-within-limits/). No key/account required. JSON API limit 20/minute; local nonblocking limiter and 429 cooldown. | `https://www.loc.gov/search/`, `fo=json`, `q`, `c=12`, `sp=1`, `fa=online-format:image`. Up to three `/item/` details with `at=item,resources`, counted as provider work. Preserve item URL, creator/contributor, collection/date/subjects, image URLs, rights/advisory/access fields. Restricted/missing-media/icon records excluded. |

## Rights evidence

No provider identity grants reuse. Openverse's [license accuracy caution](https://docs.openverse.org/api/reference/made_with_ov.html) applies: credits persist the aggregator assertion and original source; an asserted CC license is not independent legal verification. Exact CC URI normalization reuses the foundation authority. Conflicting IDs/URIs, NC/ND/SA, ambiguous Europeana rights and RightsStatements identifiers fail closed. Attribution survives candidate/schema/scene/revision/manifest and includes transformation notice.

NASA's [usage guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/) warn about third-party material. Its [actual item metadata example](https://images-assets.nasa.gov/image/as11-40-5874/metadata.json) contains item XMP fields. [Adobe defines Marked=false as public domain, omission as unknown](https://developer.adobe.com/xmp/docs/xmp-namespaces/xmp-rights/). Only a boolean false paired with matching `AVAIL:NASAID` establishes PD; conflicting rights text blocks it. NASA credit is retained. No blanket NASA permission, filename/date inference, or conversion of missing copyright into permission.

LOC accepts only explicit affirmative item public-domain statements; “no known restrictions” is insufficient. Missing statements stay unknown. Conservative exclusions can reduce coverage; widening and the existing AI/reuse/graphic chain remain the remedy.

## Bounds and cache

Two relevant providers form the primary pool; coverage is evaluated with the existing deterministic relevance, OpenCLIP shortlist and quality gate before widening. Suitability changes discovery order, never acceptance. Three logical queries, 18 physical search/detail/manifest requests, 72 verification admissions, and 12 preview/download operations remain shared per acquisition. Image dimension probes read at most 512 KiB, at most three per provider search; selected files at most 30 MiB. Unknown dimensions are never invented.

Metadata-only process cache: 128 entries, five-minute TTL, provider/normalized query/kind/public parameters key, no headers/credentials/raw envelopes, deep copies, only rights-usable candidates. Every cache admission/selection/Apply/reuse rechecks rights. Original page/media keys handle aggregator and Commons thumbnail/original duplication. No perceptual hashing.

[Openverse official throttling defaults](https://github.com/WordPress/openverse/blob/main/api/conf/settings/rest_framework.py) currently specify anonymous 5/hour and 100/day; deployment limits can differ. A conservative nonblocking hourly limiter avoids repeated requests; server 429/Retry-After remains authoritative. LOC uses its documented 20/minute. Missing Europeana key skips Europeana only. The existing Settings card and OS keyring support `EUROPEANA_API_KEY`; no settings are required for anonymous sources.

Scene `media_search.stages` records routing reasons/tiers, actual provider work,
candidate and rejection counts, cache hits, verification admissions, widening,
coverage and failures. Existing winner/fallback records and render diagnostics
retain source identity and accepted evidence, without secrets.

LOC searches digitized images across formats, including photographic maps when eligible. Download failures after strong primary coverage trigger bounded widening of only already executed query strings, skipping previously attempted provider/kind pairs.

## Validation (2026-10-02)

Mocked contract/router suite: 90 new regressions. Final backend: 1851 passed; focused media/reuse/director/critic run: 491 passed; all 42 strict-selection/fallback (Kaugummi mechanism) regressions passed. Frontend: 199 tests, typecheck, lint and production build (`next build --webpack`) passed. Ruff and `git diff --check` passed. No live provider dependency in the new contract suite.

## Real Mac retest

1. Switch to `cloud/visual-sources-retrieval-v2`, restart API/worker/web, and optionally save a Europeana key in Settings > Integrations.
2. Generate NEW projects for the previous chewing-gum question, an astronomy/spacecraft explanation, a historical event, a named landmark, an everyday action, and an anatomical mechanism. Anonymous Openverse quotas may cause widening; do not disable semantic rejection to compensate.
3. Inspect each project's `diagnostics/visual-acquisition.json`: primary pool before widening, provider requests within budget, three or fewer logical queries, rights/relevance/dedupe rejects, winning provider or existing fallback reason.
4. Verify selected real assets' manifest origin/creator/rights/policy/attribution; reopen, Change Media, Apply, and render again. Unknown NASA/LOC rights must widen/fallback rather than pass. No unrelated cross-scene reuse or text cards.
