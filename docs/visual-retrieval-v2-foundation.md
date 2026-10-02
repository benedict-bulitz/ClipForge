# Visual retrieval V2 — Phase 1 foundation

The existing staged query planner, deterministic relevance, OpenCLIP thresholds,
Visual Director fallback chain and Final Video Critic remain the selection path.
No new visual provider or routing policy is enabled in this phase.

## Rights evidence and authority

`MediaCandidate.rights` is `MediaRights`, serialized into `scene.media.rights`.
Every permission is nullable: `null` means unknown. Missing evidence is never
upgraded based on a filename, provider label, old manifest or existing file.

The evidence preserves `license_id`, `license_name`, `license_url`,
`public_domain`, `commercial_use_allowed`, `modifications_allowed`,
`attribution_required`, `attribution_text`, `rights_source`,
`rights_policy_version` and the relevant original provider metadata in `evidence`.

`evaluate_rights` is the shared copyright reuse authority. It returns `usable`,
`unusable` or `unknown`, a reason and `clipforge-commercial-edited-v1`. Only usable
assets pass. Explicit commercial/modification denials fail, including a denial
that conflicts with a public-domain claim. Otherwise public-domain evidence or
explicit commercial and modification permissions are needed. Required credits
must be persistable; unknown attribution obligations fail unless public-domain
status establishes copyright reuse. Unresolved restrictions and conflicting
Commons license metadata fail closed. Unsupported share-alike/GFDL obligations
are not accepted in this phase.

This authority checks copyright reuse for edited commercial video. It does not
claim model/property releases, endorsement permission or unlimited lawful use.

- **Pexels:** current provider-wide [Pexels License](https://www.pexels.com/license/)
  is explicitly recorded as `provider_terms`, reviewed 2026-10-01. No per-result
  license or public-domain claim is invented.
- **Pixabay:** the provider-wide [Content License terms](https://pixabay.com/service/terms/)
  are recorded as `provider_terms`, reviewed 2026-10-01. The API does not establish
  the publication date needed to claim legacy CC0. The current commercial,
  adaptation and attribution grant is used; public-domain status stays unknown.
- **Wikimedia:** `imageinfo.extmetadata` supplies the asset-specific evidence.
  PD/CC0 and exact recognized CC BY deeds can qualify. NC/ND, unsupported
  share-alike, conflicting metadata, unknown licenses or unresolved restrictions
  cannot qualify. Required credits retain creator, source page, deed URL, credit
  metadata and a ClipForge cropping/transformation note. Missing Artist is not
  filled with a fictional credit.

Provider prohibited uses and additional third-party rights still apply. Review
provider-wide evidence when terms change and advance the policy version for a
policy change.

## Persistence and acceptance boundaries

`candidate_evidence` is shared by automatic selection and Change Media. It
persists source/creator, descriptive metadata, preview/verification URLs, rights,
acceptance decision/version, canonical key and scene relevance. Automatic and
manual selections store a destination intent fingerprint for scoped visual
verification evidence. Revisions already persist these JSON values, so no DB
migration is needed. `assets.license_manifest` records the accepted evidence;
it does not grant rights by itself. Rendering refreshes policy acceptance from
retained evidence and records the asset in the manifest.

The same rights authority applies to discovery, Apply, caching, reopen, rendering
and repair/reuse. Apply checks candidate-set TTL, project/scene ownership, base
revision and current rights/scene acceptance before downloading; revision commit
uses the candidate's captured base revision and the existing concurrency guard.
Legacy caches without evidence must go through discovery again. An unsafe locked
asset is retained for correction and blocks rendering; automatic retrieval does
not replace it.

`destination_asset_allowed` reuses existing relevance, real-media quality,
coverage and protected-reveal gates. Cross-scene reuse cannot inherit another
scene's visual verdict. It honors repair `no_reuse`, rejected identity/source
keys, user locks and reveal/role constraints. The Final Critic retains its own
rendered-frame cross-scoring and persists that destination evidence when applying
a fitting base visual; its temporary retrieval constraint cannot erase a user's
repair constraint. Deliberate graphics and generated images retain their existing
provenance authority.

The subsequent [real visual regression trace](visual-selection-real-regression.md)
documents the correction of query-provenance corroboration, relaxed semantic
overrides, topic-only reuse, future-scene dedupe and diagnostic loss at retiming.

## Provider contract, registry and budget

`VisualProvider` specifies identity, capability metadata, bounded normalized
search, download and cleanup. `ProviderAdapter` adapts existing clients, whose
parsers normalize API results into `MediaCandidate`. `ProviderRegistry` exposes
enabled providers, supported kinds and the correct search/downloader; owned
clients are closed while injected clients remain caller-owned. Capability metadata
includes page limits, evidence basis and an extension point for source suitability.
No intelligent routing is implemented.

`AcquisitionBudget` is shared by every retrieval path for one scene/acquisition:

| Work | Default hard limit |
| --- | ---: |
| Provider search HTTP requests, including Pexels search retries | 18 |
| Candidates admitted to verification/preview work | 72 |
| Download admissions | 12 |

The independent maximum of **three logical query strings** stays intact.
Provider pages and existing verification preview/frame/byte limits remain
bounded. Each Pexels download admission retains its existing maximum of two HTTP
attempts. Completed verification evidence is reused, including unavailable
verification, so fallbacks do not send an asset through OpenCLIP repeatedly.
Budget exhaustion records diagnostics and proceeds through the existing Visual
Director chain. It does not create a second judge or fallback mechanism.

Only the external provider boundary categorizes timeouts, credentials, rate
limits, malformed data and unexpected adapter exceptions. Error messages omit
credentials and request URLs. Assertions/programming errors outside that boundary
are not globally swallowed.

`CandidateLedger` shares provider identity and canonical source-page dedupe
between staged, fallback and manual discovery. Canonical source keys normalize
hosts, escaped paths and tracking suffixes while preserving case-sensitive asset
paths. Generic provider landing pages are not asset identities. Persisted
`canonical_asset_key` provides an explicit cross-provider evidence extension;
no perceptual duplicate service is added.

## Mac retest

1. Fetch and check out `cloud/visual-retrieval-v2-foundation`. Leave `test` and
   `main` unchanged. Restart the API/worker and web app.
2. With Pexels configured, generate a short project. Inspect scene media and
   `assets.license_manifest` in the saved revision: rights evidence, source,
   creator, acceptance version and candidate URLs should agree.
3. Configure Pixabay and repeat with Pexels unavailable. For Commons, check one
   PD/CC0 or CC BY asset, an asset with missing rights, and an unsupported license.
   Only accepted rights should reach selected scene media.
4. Open Change Media, choose an alternative, Apply, close and reopen the project,
   then render/export. Evidence and required credit must survive. Fetch another
   candidate, edit the project and try its old token: expect a revision conflict.
5. Reopen a legacy project with an existing real-media file but no rights. Retry
   discovery. The file must not bypass rights validation. Locked unsafe media
   should require a new user choice.
6. Exercise repair `no_reuse`, rejected media, a pre-reveal scene, and scenes with
   unrelated subjects. An existing file must not permit borrowing. Two scenes
   with a fitting intentional base visual should still allow continuity.
7. Simulate an unavailable/rate-limited provider and exhaust a small test budget.
   Other configured providers/fallbacks should continue; inspect `media_search`
   query/request accounting and `acquisition_budget` limits. No paid generation
   is required by the automated tests.
