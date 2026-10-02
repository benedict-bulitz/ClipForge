# Real multi-source regression audit — 2026-10-02

Base: `fdc3b6b`. Four exact-prompt projects were read from `apps/api/clipforge.db` in read-only mode, revisions 1–4, and each saved `diagnostics/visual-acquisition.json`. Frames were inspected from retained `audio-layers/v2/picture.mp4`. Export revision 2 is the rendered source. Export cleanup deleted the original assets, renders, segment caches and critic frame files. The exported Downloads files are absent here; the retained picture video confirms the reported visuals.

The current revision scene list differs from the final render layout after retiming/completion in the fridge and gum projects. Playback times and identities below use the render layout, joined to the matching diagnostic scene evidence, rather than assuming the current revision scenes alone describe the video.

Unselected candidate payloads were not persisted by the old diagnostics. Provider return/rejection counts are available, but individual unselected titles and scores cannot be recovered. No live provider requests were made to fabricate the old pool.

## Proven causes and fixes

- Pexels videos had empty title/description/tags even where the actual source-page URL named a street or fashion subject. Unknown metadata could pass on a marginal scene score. The supplied Pexels page caption is now semantic evidence in the shared relevance authority, including cached assets, manual candidates and critic reuse. Search query text cannot supply additional semantic matches.
- Commons long descriptions included transcribed newspapers, book copy and leaflets. Incidental sentence words were scored as visual evidence; unavailable previews allowed a metadata-only acceptance. Scoring now bounds caption/summary/tag evidence; no topic-specific or newly learned German-word exclusions are added. Complete original metadata remains persisted; it is not deleted or used as rights evidence.
- Router ignored actual script `block.text`, read entire visual-intent dictionaries including exclusions, omitted positive primary-answer context, and only recognized years before 1950. Positive intent and linked block/fact context now inform routing; planetary/atmospheric evidence prefers NASA, and twentieth-century historical dates retain archival priority. Exclusions and unrelated units do not select providers.
- Existing relevance did not require evidence of the requested historical period. The shared metadata authority now evaluates dates and archival evidence against the scene, full parent block and linked facts. Current-location similarity alone cannot override a missing/conflicting period. Explicit present-day B-roll remains eligible.
- Provider rank scales differed (Pexels technical/rank values versus new provider page ranks). The trace does not prove this caused these winners. Ranks are now compared only relative to each provider page, in the shared shortlist, routed selection and manual ordering; absolute rank cannot consume the entire verification shortlist when equivalent evidence exists from another source.
- OpenCLIP scene prompts retain canonical action/setting before search-query text. Same verifier, 0.24 scene threshold, presentation check, Visual Director, AI budget, rights policy and Final Critic are reused. No new judge, paid candidate calls, topic-specific rejection lists or synthetic text cards were added.
- New diagnostics retain at most 24 candidate evidence records per logical query, including bounded metadata, rights decisions, temporal evidence, verification and acceptance. Existing work limits and the three-query limit remain.

## Scene evidence

### Warum ist der Himmel auf dem Mars rot?

Project: `081a8fd8-d80e-49ee-9c56-dd86ed00ecee`. Created 2026-10-02T12:01:51.192624+00:00; export 2026-10-02T12:31:26.565719+00:00.

#### scene_01_01 — 0.0–3.467 s

Assessment: **ACCEPTABLE — terrestrial soil texture supports the hook; not actual Mars evidence**.

- Narration: Der rote Marsboden schickt feinen Staub bis in den Himmel.
- Visual intent: {"visual_goal": "macro-to-wide transition from ground texture to open sky of fine rust-colored dust lifting from dry rocky ground, dust rises from the surface and drifts upward, tiny red particles catching light above cracked soil", "objects": ["fine rust-colored dust lifting from dry rocky ground"], "actions": ["dust rises from the surface and drifts upward"], "context": ["tiny red particles catching light above cracked soil", "detailed rust-colored ground versus soft red haze overhead"], "source": "triple_hook_v2"}
- Query: `rust colored dust lifting dry ground`. Planned queries: ["rust colored dust lifting dry ground", "red dust particles blowing upward desert", "close red dusty soil wind"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `rust colored dust lifting dry ground` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=5, openverse/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=1, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=partial; `red dust particles blowing upward desert` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=12, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=partial
- Winning provider / ID: `pexels` / `12303811`.
- Source: https://www.pexels.com/photo/brown-soil-in-close-up-shot-12303811/
- Candidate title: Detailed texture of cracked, dry soil with scattered debris, showcasing natural erosion.
- Description excerpt: Detailed texture of cracked, dry soil with scattered debris, showcasing natural erosion.
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": 150.0, "confidence": "high", "scene_matches": ["cracked", "detailed", "dry", "soil", "texture"], "subject_matches": [], "query_provenance": true, "selection_tier": 3}
- OpenCLIP: {"status": "verified", "score": 0.30516511425375936, "subject_score": 0.2878631353378296, "scene_score": 0.30821840465068817, "provenance": "provider_thumbnail", "frame_scores": [], "frame_count": 1, "presentation_score": 0.12065986543893814, "photographic_score": 0.1860000193119049, "diagram_score": 0.1283872164785862, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `photo_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_photo", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_02_01 — 3.467–5.9 s

Assessment: **GOOD — labeled AI-generated dusty planetary landscape**.

- Narration: In der Marsluft schwebt feiner roter Staub.
- Visual intent: {"visual_goal": "Marsluft schwebt feiner roter Staub", "objects": ["Marsluft", "schwebt", "feiner"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `Fine reddish dust particles suspended in the Martian atmosphere A thin cloud of red dust drifting through the air; Rocky Martian landscape beneath a hazy red sky; Rust-colored dust visibly hangs above`. Planned queries: ["marsluft schwebt feiner roter staub", "himmel auf dem mars rot"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `marsluft schwebt feiner roter staub` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none; `himmel auf dem mars rot` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=9, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none
- Winning provider / ID: `generated_openai` / `eeb05434d239888a7b13`.
- Source: AI-generated; no external source page
- Candidate title: AI-generated image: Fine reddish dust particles suspended in the Martian atmosphere A thin cloud of red dust drifting through the air; Rocky Martian landscape beneath a hazy red sky; Rust-colored dust visibly hangs above
- Description excerpt: Fine reddish dust particles suspended in the Martian atmosphere A thin cloud of red dust drifting through the air; Rocky Martian landscape beneath a hazy red sky; Rust-colored dust visibly hangs above
- Tags: []
- Rights: Existing generated-image provenance; not stock rights. Acceptance: None.
- Metadata relevance: {"score": null, "confidence": "generated", "scene_matches": null, "subject_matches": null, "query_provenance": null, "selection_tier": null}
- OpenCLIP: {"status": "verified", "accepted": true, "verified": true, "score": 0.3787251174449921, "scene_score": 0.3874693512916565, "presentation_risk": false, "threshold": 0.24}
- Acceptance: null; source `generated_image`; asset status `generated_image_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "GENERATE_FALLBACK", "resolved_type": "generated_image", "decision_reason": "no_accepted_real_media:plan_exhausted:none", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_03_01 — 5.9–9.367 s

Assessment: **ACCEPTABLE / WEAK — rocky landscape provides atmosphere but little direct iron-oxide evidence**.

- Narration: Er ist vor allem deshalb rot, weil er Eisenoxid enthält.
- Visual intent: {"visual_goal": "allem deshalb weil Eisenoxid enthält", "objects": ["allem", "deshalb", "weil"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `himmel auf dem mars rot`. Planned queries: ["himmel auf dem mars rot"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `himmel auf dem mars rot` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=5, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=13, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=partial
- Winning provider / ID: `pexels` / `8474500`.
- Source: https://www.pexels.com/photo/dry-rocky-land-under-a-gloomy-sky-8474500/
- Candidate title: A barren and rocky desert landscape reminiscent of the planet Mars with a warm, dusty sky.
- Description excerpt: A barren and rocky desert landscape reminiscent of the planet Mars with a warm, dusty sky.
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": 16.0, "confidence": "acceptable", "scene_matches": ["mar"], "subject_matches": ["mar"], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.2706619530916214, "subject_score": 0.2872486710548401, "scene_score": 0.2677348852157593, "provenance": "provider_thumbnail", "frame_scores": [], "frame_count": 1, "presentation_score": 0.1136375293135643, "photographic_score": 0.18864906579256058, "diagram_score": 0.10348865762352943, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `photo_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_photo", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_04_01 — 9.367–11.1 s

Assessment: **WRONG — town street cannot show rust/mineral explanation**.

- Narration: Das kennst du als Rost.
- Visual intent: {"visual_goal": "kennst Rost", "objects": ["kennst", "Rost"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `kennst rost`. Planned queries: ["kennst rost", "himmel auf dem mars rot"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `kennst rost` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none; `himmel auf dem mars rot` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=14, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none
- Winning provider / ID: `pexels` / `38235492`.
- Source: https://www.pexels.com/video/charming-cobblestone-street-in-quaint-village-38235492/
- Candidate title:
- Description excerpt:
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": -60.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": [], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.23239313066005707, "subject_score": 0.1692064106464386, "scene_score": 0.24304994940757751, "provenance": "provider_video_frames", "frame_scores": [0.23420505784451962, 0.23239313066005707, 0.22253460139036177], "frame_count": 3, "presentation_score": 0.10653037950396538, "photographic_score": 0.17300527542829514, "diagram_score": 0.08108359575271606, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `relaxed_fallback`; asset status `video_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_video", "decision_reason": "real_media_relaxed_fallback", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_05_01 — 11.1–14.2 s

Assessment: **GOOD — AI-generated dust whirl supports dust transport**.

- Narration: Dieser Roststaub wirbelt vom Boden durch die Atmosphäre und
- Visual intent: {"visual_goal": "Dieser Roststaub wirbelt Boden durch Atmosphäre", "objects": ["Dieser", "Roststaub", "wirbelt"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `Fine rusty dust lifting from the Martian ground A windblown cloud of red-orange dust swirls upward through the thin atmosphere; Rocky Martian landscape beneath a hazy red sky; Iron-rich dust coating t`. Planned queries: ["roststaub wirbelt boden durch atmosphäre", "roststaub wirbelt boden durch atmosphäre vom", "himmel auf dem mars rot"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `roststaub wirbelt boden durch atmosphäre` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none; `roststaub wirbelt boden durch atmosphäre vom` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=8, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none; `himmel auf dem mars rot` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=14, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none
- Winning provider / ID: `generated_openai` / `f8da2cc96be41b1520d7`.
- Source: AI-generated; no external source page
- Candidate title: AI-generated image: Fine rusty dust lifting from the Martian ground A windblown cloud of red-orange dust swirls upward through the thin atmosphere; Rocky Martian landscape beneath a hazy red sky; Iron-rich dust coating t
- Description excerpt: Fine rusty dust lifting from the Martian ground A windblown cloud of red-orange dust swirls upward through the thin atmosphere; Rocky Martian landscape beneath a hazy red sky; Iron-rich dust coating t
- Tags: []
- Rights: Existing generated-image provenance; not stock rights. Acceptance: None.
- Metadata relevance: {"score": null, "confidence": "generated", "scene_matches": null, "subject_matches": null, "query_provenance": null, "selection_tier": null}
- OpenCLIP: {"status": "verified", "accepted": true, "verified": true, "score": 0.3413594886660576, "scene_score": 0.3692326247692108, "presentation_risk": false, "threshold": 0.24}
- Acceptance: null; source `generated_image`; asset status `generated_image_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "GENERATE_FALLBACK", "resolved_type": "generated_image", "decision_reason": "no_accepted_real_media:budget_exhausted:none", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_05_02 — 14.2–16.967 s

Assessment: **WEAK / WRONG — starry night is not the red dusty daytime atmosphere**.

- Narration: lässt den Himmel auf dem Mars rot erscheinen.
- Visual intent: {"visual_goal": "lässt Himmel Mars erscheinen", "objects": ["lässt", "Himmel", "Mars"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `lässt himmel mars erscheinen`. Planned queries: ["lässt himmel mars erscheinen", "lässt himmel mars erscheinen den auf", "himmel auf dem mars rot"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `lässt himmel mars erscheinen` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited]; coverage=strong
- Winning provider / ID: `pexels` / `36434286`.
- Source: https://www.pexels.com/video/starry-night-sky-with-shooting-stars-36434286/
- Candidate title:
- Description excerpt:
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": -60.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": [], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.28073284104466434, "subject_score": 0.24288418889045715, "scene_score": 0.2870737463235855, "provenance": "provider_video_frames", "frame_scores": [0.2856160879135132, 0.28073284104466434, 0.27583018243312835], "frame_count": 3, "presentation_score": 0.13779297471046448, "photographic_score": 0.19857492297887802, "diagram_score": 0.12742309272289276, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `video_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_video", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}


### Warum kann man Kaugummi kauen ohne dass er zerfällt?

Project: `41147aaa-9cf2-4505-9259-260efb34812b`. Created 2026-10-02T12:24:01.464531+00:00; export 2026-10-02T12:31:46.136594+00:00.

#### scene_01_01 — 0.0–1.867 s

Assessment: **GOOD but repetitive — mouth/gum concept requested by plan**.

- Narration: Stundenlanges Kauen zerreißt Kaugummi nicht.
- Visual intent: {"visual_goal": "Extreme close-up of mouth and jaw from the side of A person chewing gum, Slowly chewing with a calm, steady rhythm, A small gum bubble gently stretches and returns to shape", "objects": ["A person chewing gum"], "actions": ["Slowly chewing with a calm, steady rhythm"], "context": ["A small gum bubble gently stretches and returns to shape", "Constant chewing motion versus gum remaining whole"], "source": "triple_hook_v2"}
- Query: `extreme close person chewing gum side`. Planned queries: ["extreme close person chewing gum side", "gum bubble close stretching", "chewing gum mouth close"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `extreme close person chewing gum side` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=3, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited]; coverage=strong
- Winning provider / ID: `pexels` / `7299470`.
- Source: https://www.pexels.com/video/extreme-close-up-view-of-a-man-s-mouth-7299470/
- Candidate title:
- Description excerpt:
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": -60.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": [], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.2782144442200661, "subject_score": 0.2587217688560486, "scene_score": 0.28165432810783386, "provenance": "provider_video_frames", "frame_scores": [0.2782144442200661, 0.2822741813957691, 0.27532672733068464], "frame_count": 3, "presentation_score": 0.13084156811237335, "photographic_score": 0.19387029856443405, "diagram_score": 0.13183486089110374, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `video_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_video", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_02_01 — 1.867–7.067 s

Assessment: **GOOD but repetitive — mouth/gum concept requested by plan**.

- Narration: Kaugummi ist eine weiche, flexible Masse, die sich beim Kauen immer wieder verformen lässt.
- Visual intent: {"visual_goal": "Kaugummi eine weiche flexible Masse sich", "objects": ["Kaugummi", "eine", "weiche"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `A piece of chewing gum Stretching and folding between the teeth as it is chewed; Close-up inside a person's mouth; Soft pink gum visibly flattened between white molars`. Planned queries: ["kaugummi weiche flexible masse kauen immer", "kann kaugummi kauen zerfällt"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `kaugummi weiche flexible masse kauen immer` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none; `kann kaugummi kauen zerfällt` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=6, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none
- Winning provider / ID: `generated_openai` / `5f6f7bc28a65085867de`.
- Source: AI-generated; no external source page
- Candidate title: AI-generated image: A piece of chewing gum Stretching and folding between the teeth as it is chewed; Close-up inside a person's mouth; Soft pink gum visibly flattened between white molars
- Description excerpt: A piece of chewing gum Stretching and folding between the teeth as it is chewed; Close-up inside a person's mouth; Soft pink gum visibly flattened between white molars
- Tags: []
- Rights: Existing generated-image provenance; not stock rights. Acceptance: None.
- Metadata relevance: {"score": null, "confidence": "generated", "scene_matches": null, "subject_matches": null, "query_provenance": null, "selection_tier": null}
- OpenCLIP: {"status": "verified", "accepted": true, "verified": true, "score": 0.3100148886442184, "scene_score": 0.33443161845207214, "presentation_risk": false, "threshold": 0.24}
- Acceptance: null; source `generated_image`; asset status `generated_image_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "GENERATE_FALLBACK", "resolved_type": "generated_image", "decision_reason": "no_accepted_real_media:plan_exhausted:none", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_03_01 — 7.067–10.4 s

Assessment: **GOOD but repetitive — mouth/gum concept requested by plan**.

- Narration: Weil sie sich dabei verformen lässt, ohne sich aufzulösen,
- Visual intent: {"visual_goal": "Weil sich dabei verformen lässt ohne", "objects": ["Weil", "sich", "dabei"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `A single piece of chewing gum Stretching and folding while remaining as one intact, elastic piece between visible teeth; Close-up inside a person’s mouth; Gum is visibly deformed but not breaking apar`. Planned queries: ["weil verformen lässt sie aufzulösen", "kann kaugummi kauen zerfällt"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `weil verformen lässt sie aufzulösen` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none; `kann kaugummi kauen zerfällt` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none
- Winning provider / ID: `generated_openai` / `61aba0bdcf1c6276e6f5`.
- Source: AI-generated; no external source page
- Candidate title: AI-generated image: A single piece of chewing gum Stretching and folding while remaining as one intact, elastic piece between visible teeth; Close-up inside a person’s mouth; Gum is visibly deformed but not breaking apar
- Description excerpt: A single piece of chewing gum Stretching and folding while remaining as one intact, elastic piece between visible teeth; Close-up inside a person’s mouth; Gum is visibly deformed but not breaking apar
- Tags: []
- Rights: Existing generated-image provenance; not stock rights. Acceptance: None.
- Metadata relevance: {"score": null, "confidence": "generated", "scene_matches": null, "subject_matches": null, "query_provenance": null, "selection_tier": null}
- OpenCLIP: {"status": "verified", "accepted": true, "verified": true, "score": 0.31967159882187846, "scene_score": 0.3173389285802841, "presentation_risk": false, "threshold": 0.24}
- Acceptance: null; source `generated_image`; asset status `generated_image_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "GENERATE_FALLBACK", "resolved_type": "generated_image", "decision_reason": "no_accepted_real_media:plan_exhausted:none", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_03_02 — 10.4–13.33 s

Assessment: **GOOD but repetitive — mouth/gum concept requested by plan**.

- Narration: zerfällt der Kaugummi auch nach stundenlangem Kauen nicht.
- Visual intent: {"visual_goal": "zerfällt Kaugummi auch nach stundenlangem Kauen", "objects": ["zerfällt", "Kaugummi", "auch"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `A single piece of chewing gum Stretching and folding while remaining as one intact, elastic piece between visible teeth; Close-up inside a person’s mouth; Gum is visibly deformed but not breaking apar`. Planned queries: ["zerfällt kaugummi nach stundenlangem kauen", "kann kaugummi kauen zerfällt"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `zerfällt kaugummi nach stundenlangem kauen` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none; `kann kaugummi kauen zerfällt` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=4, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=none
- Winning provider / ID: `generated_openai` / `61aba0bdcf1c6276e6f5`.
- Source: AI-generated; no external source page
- Candidate title: AI-generated image: A single piece of chewing gum Stretching and folding while remaining as one intact, elastic piece between visible teeth; Close-up inside a person’s mouth; Gum is visibly deformed but not breaking apar
- Description excerpt: A single piece of chewing gum Stretching and folding while remaining as one intact, elastic piece between visible teeth; Close-up inside a person’s mouth; Gum is visibly deformed but not breaking apar
- Tags: []
- Rights: Existing generated-image provenance; not stock rights. Acceptance: None.
- Metadata relevance: {"score": null, "confidence": "generated", "scene_matches": null, "subject_matches": null, "query_provenance": null, "selection_tier": null}
- OpenCLIP: {"status": "verified", "accepted": true, "verified": true, "score": 0.31967159882187846, "scene_score": 0.3173389285802841, "presentation_risk": false, "threshold": 0.24}
- Acceptance: null; source `generated_image`; asset status `generated_image_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "GENERATE_FALLBACK", "resolved_type": "generated_image", "decision_reason": "no_accepted_real_media:plan_exhausted:none", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}


### Warum wurde die Berliner Mauer gebaut?

Project: `484ed264-7ed8-4f84-bd58-2f5dbb9eb935`. Created 2026-10-02T12:08:08.377146+00:00; export 2026-10-02T12:31:35.896762+00:00.

#### scene_01_01 — 0.0–3.267 s

Assessment: **WRONG as 1961 evidence — present city/TV-tower street; aesthetically plausible location only**.

- Narration: Am 13. August 1961 begann der Mauerbau –
- Visual intent: {"visual_goal": "low wide shot from behind pedestrians facing the barrier of archival Berlin street as workers unroll barbed wire and block a crossing, a street crossing is closed while pedestrians stop at a distance, a fresh barbed-wire", "objects": ["archival Berlin street as workers unroll barbed wire and block a crossing"], "actions": ["a street crossing is closed while pedestrians stop at a distance"], "context": ["a fresh barbed-wire coil stretched across the street", "an ordinary city street suddenly cut off"], "source": "triple_hook_v2"}
- Query: `berlin august barbed wire street archival`. Planned queries: ["berlin august barbed wire street archival", "berlin wall construction street barrier", "berlin border closure historical photo"].
- Routed sources (unique): loc (photo: historical_or_archival), wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `berlin august barbed wire street archival` [loc/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, failure=invalid_credentials, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=4]; coverage=strong
- Winning provider / ID: `pexels` / `38738787`.
- Source: https://www.pexels.com/video/street-view-with-berlin-tv-tower-in-background-38738787/
- Candidate title:
- Description excerpt:
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": -60.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": [], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.29353901892900464, "subject_score": 0.3261447548866272, "scene_score": 0.289385586977005, "provenance": "provider_video_frames", "frame_scores": [0.2818435400724411, 0.29353901892900464, 0.30006372183561325], "frame_count": 3, "presentation_score": 0.11319775134325027, "photographic_score": 0.22849681973457336, "diagram_score": 0.11773932352662086, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `video_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_video", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_01_02 — 3.267–6.533 s

Assessment: **WEAK — railway reflection has no independently established historical/explanatory fit**.

- Narration: doch die entscheidende Geschichte hatte viel früher begonnen.
- Visual intent: {"visual_goal": "low wide shot from behind pedestrians facing the barrier of archival Berlin street as workers unroll barbed wire and block a crossing, a street crossing is closed while pedestrians stop at a distance, a fresh barbed-wire", "objects": ["archival Berlin street as workers unroll barbed wire and block a crossing"], "actions": ["a street crossing is closed while pedestrians stop at a distance"], "context": ["a fresh barbed-wire coil stretched across the street", "an ordinary city street suddenly cut off"], "source": "triple_hook_v2"}
- Query: `berlin august barbed wire street archival`. Planned queries: ["berlin august barbed wire street archival", "berlin wall construction street barrier", "berlin border closure historical photo"].
- Routed sources (unique): wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `berlin august barbed wire street archival` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=4]; coverage=strong
- Winning provider / ID: `pexels` / `20610687`.
- Source: https://www.pexels.com/video/u-bahn-spiegelung-20610687/
- Candidate title:
- Description excerpt:
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": -60.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": [], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.271477859467268, "subject_score": 0.27510419487953186, "scene_score": 0.2708379179239273, "provenance": "provider_video_frames", "frame_scores": [0.2519139122217893, 0.271477859467268, 0.2850204549729824], "frame_count": 3, "presentation_score": 0.12108464911580086, "photographic_score": 0.24353372305631638, "diagram_score": 0.11797812208533287, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `video_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_video", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_02_01 — 6.533–9.0 s

Assessment: **WEAK — modern snowy Berlin location, no migration or construction evidence**.

- Narration: Die DDR baute die Mauer, weil
- Visual intent: {"visual_goal": "baute Mauer weil immer mehr Menschen", "objects": ["baute", "Mauer", "weil"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `baute mauer weil immer menschen ddr`. Planned queries: ["baute mauer weil immer menschen", "baute mauer weil immer menschen ddr", "wurde berliner mauer gebaut"].
- Routed sources (unique): loc (photo: historical_or_archival), wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `baute mauer weil immer menschen` [loc/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, failure=invalid_credentials, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=4, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15]; coverage=partial; `baute mauer weil immer menschen ddr` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=5]; coverage=strong
- Winning provider / ID: `pexels` / `35683781`.
- Source: https://www.pexels.com/video/snowfall-in-berlin-serene-winter-urban-scene-35683781/
- Candidate title:
- Description excerpt:
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": -60.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": [], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.2595870181918144, "subject_score": 0.18674716353416443, "scene_score": 0.2725689858198166, "provenance": "provider_video_frames", "frame_scores": [0.2725238613784313, 0.2595870181918144, 0.2591025769710541], "frame_count": 3, "presentation_score": 0.11905692517757416, "photographic_score": 0.19658398628234863, "diagram_score": 0.11801565438508987, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `video_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_video", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual", "simple_graphic"]}

#### scene_02_02 — 9.0–11.433 s

Assessment: **WRONG — fur-art book cover, not people fleeing**.

- Narration: immer mehr Menschen das Land verließen.
- Visual intent: {"visual_goal": "immer mehr Menschen Land verließen", "objects": ["immer", "mehr", "Menschen"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `immer menschen`. Planned queries: ["immer menschen verliessen", "wurde berliner mauer gebaut"].
- Routed sources (unique): wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `immer menschen verliessen` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15]; coverage=none; `wurde berliner mauer gebaut` [wikimedia/photo: calls=1, returned=5, rights-rejects=5, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=13]; coverage=none
- Winning provider / ID: `wikimedia` / `22589123`.
- Source: https://commons.wikimedia.org/wiki/File:Ein_Pelz_war_immer_dabei,_Buchdeckel.jpg
- Candidate title: File:Ein Pelz war immer dabei, Buchdeckel.jpg
- Description excerpt: Ein Pelz war immer dabei. Der Pelz in der bildenden Kunst des 18.-20. Jahrhunderts. Herausgegeben durch Richard Franke. Rifra-Verlag Murrhardt. Überreicht durch WEPE Pelzkonfektion. Dieses Buch erscheint in einer kleinen Auflage und ist im Handel nicht erhä
- Tags: []
- Rights: {"license_id": "pd", "public_domain": true, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "wikimedia_extmetadata:https://commons.wikimedia.org/wiki/File:Ein_Pelz_war_immer_dabei,_Buchdeckel.jpg", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": 46.0, "confidence": "acceptable", "scene_matches": ["immer", "menschen"], "subject_matches": ["menschen"], "query_provenance": false, "selection_tier": 3}
- OpenCLIP: {"status": "unavailable_preview", "score": null, "subject_score": null, "scene_score": null, "provenance": null, "frame_scores": [], "frame_count": 0, "presentation_score": null, "photographic_score": null, "diagram_score": null, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "metadata_match", "authority": "real_media_quality_gate"}; source `relaxed_fallback`; asset status `photo_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_photo", "decision_reason": "real_media_relaxed_fallback", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual", "simple_graphic"]}

#### scene_03_01 — 11.433–14.7 s

Assessment: **WEAK — current city architecture cannot establish 1949–1961 migration**.

- Narration: Zwischen 1949 und 1961 flohen mehr als 2,5
- Visual intent: {"visual_goal": "Zwischen 1949 1961 flohen mehr", "objects": ["Zwischen", "1949", "1961"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `wurde berliner mauer gebaut`. Planned queries: ["zwischen flohen", "wurde berliner mauer gebaut"].
- Routed sources (unique): wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `zwischen flohen` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15]; coverage=none; `wurde berliner mauer gebaut` [wikimedia/photo: calls=1, returned=5, rights-rejects=5, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=14]; coverage=partial
- Winning provider / ID: `pexels` / `12475268`.
- Source: https://www.pexels.com/photo/concrete-buildings-and-a-tower-under-cloudy-sky-12475268/
- Candidate title: Street view of Berlin with the iconic TV tower against a cloudy sky, showcasing urban architecture.
- Description excerpt: Street view of Berlin with the iconic TV tower against a cloudy sky, showcasing urban architecture.
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": 13.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": ["berlin", "street"], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.2712680038064718, "subject_score": 0.3611367344856262, "scene_score": 0.2554088160395622, "provenance": "provider_thumbnail", "frame_scores": [], "frame_count": 1, "presentation_score": 0.09047575294971466, "photographic_score": 0.14207573235034943, "diagram_score": 0.0884859748184681, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `photo_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_photo", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual", "simple_graphic"]}

#### scene_03_02 — 14.7–17.967 s

Assessment: **WRONG — 1970s anti-nuclear leaflet, not DDR migration**.

- Narration: Millionen Menschen aus der DDR in die Bundesrepublik.
- Visual intent: {"visual_goal": "Millionen Menschen Bundesrepublik", "objects": ["Millionen", "Menschen", "Bundesrepublik"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `millionen menschen bundesrepublik`. Planned queries: ["millionen menschen bundesrepublik", "millionen menschen bundesrepublik ddr", "wurde berliner mauer gebaut"].
- Routed sources (unique): wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `millionen menschen bundesrepublik` [wikimedia/photo: calls=1, returned=5, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited]; coverage=strong
- Winning provider / ID: `wikimedia` / `192589585`.
- Source: https://commons.wikimedia.org/wiki/File:%27So_sieht_unsere_strahlende_Zukunft_aus_-_deshalb_heute_aktiv_-_sonst_morgen_radioaktiv%27_-_aktion_pro_vita_Stuttgart_---_1970er.JPG
- Candidate title: File:'So sieht unsere strahlende Zukunft aus - deshalb heute aktiv - sonst morgen radioaktiv' - aktion pro vita Stuttgart --- 1970er.JPG
- Description excerpt: Flugblatt gegen Atomkraft der "aktion pro vita Stuttgart " in den 1970er Jahren Aufschrift (Vorderseite): So sieht unsere „strahlende“ Zukunft aus! Deshalb: heute aktiv - sonst morgen radioaktiv ! Ein Kind aus Hiroshima Fruchtschäden (Injury of fetus = Verl
- Tags: []
- Rights: {"license_id": "cc-by-3.0", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": true, "rights_source": "wikimedia_extmetadata:https://commons.wikimedia.org/wiki/File:%27So_sieht_unsere_strahlende_Zukunft_aus_-_deshalb_heute_aktiv_-_sonst_morgen_radioaktiv%27_-_aktion_pro_vita_Stuttgart_---_1970er.JPG", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": 106.0, "confidence": "high", "scene_matches": ["aus", "bundesrepublik", "menschen", "millionen"], "subject_matches": ["menschen"], "query_provenance": true, "selection_tier": 3}
- OpenCLIP: {"status": "unavailable_preview", "score": null, "subject_score": null, "scene_score": null, "provenance": null, "frame_scores": [], "frame_count": 0, "presentation_score": null, "photographic_score": null, "diagram_score": null, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "metadata_match", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `photo_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_photo", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual", "simple_graphic"]}

#### scene_04_01 — 17.967–20.833 s

Assessment: **WEAK — current Friedrichstraße station street, no economic-collapse evidence**.

- Narration: Dadurch drohte der DDR ein wirtschaftlicher Zusammenbruch.
- Visual intent: {"visual_goal": "Dadurch drohte wirtschaftlicher Zusammenbruch", "objects": ["Dadurch", "drohte", "wirtschaftlicher"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `dadurch drohte wirtschaftlicher zusammenbruch`. Planned queries: ["dadurch drohte wirtschaftlicher zusammenbruch", "dadurch drohte wirtschaftlicher zusammenbruch ddr", "wurde berliner mauer gebaut"].
- Routed sources (unique): wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `dadurch drohte wirtschaftlicher zusammenbruch` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=14, rights-rejects=0, relevance-rejects=13]; coverage=partial; `dadurch drohte wirtschaftlicher zusammenbruch ddr` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=14]; coverage=partial
- Winning provider / ID: `pexels` / `36885925`.
- Source: https://www.pexels.com/photo/street-view-of-bahnhof-friedrichstrasse-berlin-36885925/
- Candidate title: Empty street leading to Berlin's Bahnhof Friedrichstrasse on a clear day.
- Description excerpt: Empty street leading to Berlin's Bahnhof Friedrichstrasse on a clear day.
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": 13.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": ["berlin", "street"], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.2410584844648838, "subject_score": 0.1961888074874878, "scene_score": 0.2489766627550125, "provenance": "provider_thumbnail", "frame_scores": [], "frame_count": 1, "presentation_score": 0.1438964568078518, "photographic_score": 0.19261110574007034, "diagram_score": 0.12220802903175354, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `photo_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_photo", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual", "simple_graphic"]}

#### scene_05_01 — 20.833–23.7 s

Assessment: **GOOD / ACCEPTABLE — AI illustration of barrier construction, not archival proof**.

- Narration: August 1961 begann die DDR deshalb mit
- Visual intent: {"visual_goal": "August 1961 begann deshalb", "objects": ["August", "1961", "begann"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `A newly built concrete barrier across a Berlin street East German workers stack concrete blocks and stretch barbed wire while pedestrians st; East Berlin, August 1961, beside apartment buildings and a`. Planned queries: ["wurde berliner mauer gebaut"].
- Routed sources (unique): wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `wurde berliner mauer gebaut` [wikimedia/photo: calls=1, returned=5, rights-rejects=5, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=13]; coverage=none
- Winning provider / ID: `generated_openai` / `3d93f70009475d357063`.
- Source: AI-generated; no external source page
- Candidate title: AI-generated image: A newly built concrete barrier across a Berlin street East German workers stack concrete blocks and stretch barbed wire while pedestrians st; East Berlin, August 1961, beside apartment buildings and a
- Description excerpt: A newly built concrete barrier across a Berlin street East German workers stack concrete blocks and stretch barbed wire while pedestrians st; East Berlin, August 1961, beside apartment buildings and a
- Tags: []
- Rights: Existing generated-image provenance; not stock rights. Acceptance: None.
- Metadata relevance: {"score": null, "confidence": "generated", "scene_matches": null, "subject_matches": null, "query_provenance": null, "selection_tier": null}
- OpenCLIP: {"status": "verified", "accepted": true, "verified": true, "score": 0.2780808165669441, "scene_score": 0.27949684858322144, "presentation_risk": false, "threshold": 0.24}
- Acceptance: null; source `generated_image`; asset status `generated_image_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "GENERATE_FALLBACK", "resolved_type": "generated_image", "decision_reason": "no_accepted_real_media:plan_exhausted:none", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_05_02 — 23.7–26.567 s

Assessment: **WRONG — critic borrows modern snowy city for construction beat**.

- Narration: dem Bau: Die Mauer sollte die Menschen
- Visual intent: {"visual_goal": "Mauer sollte Menschen daran hindern verlassen", "objects": ["Mauer", "sollte", "Menschen"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `baute mauer weil immer menschen ddr`. Planned queries: ["mauer sollte menschen daran hindern verlassen", "wurde berliner mauer gebaut"].
- Routed sources (unique): wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (photo: historical_or_archival), pexels (video: alternate_media_kind)
- Performed work: `mauer sollte menschen daran hindern verlassen` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=14, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6]; coverage=none; `wurde berliner mauer gebaut` [wikimedia/photo: calls=1, returned=5, rights-rejects=5, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=13, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6]; coverage=none
- Winning provider / ID: `pexels` / `35683781`.
- Source: https://www.pexels.com/video/snowfall-in-berlin-serene-winter-urban-scene-35683781/
- Candidate title:
- Description excerpt:
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": -60.0, "confidence": "unknown", "scene_matches": [], "subject_matches": [], "query_provenance": false, "selection_tier": 0}
- OpenCLIP: {"status": "verified", "score": 0.2696779668331146, "scene_score": 0.2696779668331146, "provenance": "final_critic_destination_frames"}
- Acceptance: null; source `generated_image`; asset status `block_visual_continued`.
- Director / reuse: {"planned_type": "stock_photo", "decision": "GENERATE_FALLBACK", "resolved_type": "reuse_previous_visual", "decision_reason": "final_critic_base_visual", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_05_03 — 26.567–28.967 s

Assessment: **ACCEPTABLE — fitting generated barrier continuity**.

- Narration: daran hindern, die DDR zu verlassen.
- Visual intent: {"visual_goal": "daran hindern verlassen", "objects": ["daran", "hindern", "verlassen"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `A newly built concrete barrier across a Berlin street East German workers stack concrete blocks and stretch barbed wire while pedestrians st; East Berlin, August 1961, beside apartment buildings and a`. Planned queries: ["daran hindern verlassen", "daran hindern verlassen ddr", "wurde berliner mauer gebaut"].
- Routed sources (unique): wikimedia (photo: historical_or_archival), openverse (photo: historical_or_archival), pexels (video: historical_or_archival), pexels (photo: alternate_media_kind)
- Performed work: `daran hindern verlassen` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15]; coverage=none; `daran hindern verlassen ddr` [wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=10]; coverage=none; `wurde berliner mauer gebaut` [wikimedia/photo: calls=1, returned=5, rights-rejects=5, relevance-rejects=0, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=14]; coverage=none
- Winning provider / ID: `generated_openai` / `3d93f70009475d357063`.
- Source: AI-generated; no external source page
- Candidate title: AI-generated image: A newly built concrete barrier across a Berlin street East German workers stack concrete blocks and stretch barbed wire while pedestrians st; East Berlin, August 1961, beside apartment buildings and a
- Description excerpt: A newly built concrete barrier across a Berlin street East German workers stack concrete blocks and stretch barbed wire while pedestrians st; East Berlin, August 1961, beside apartment buildings and a
- Tags: []
- Rights: Existing generated-image provenance; not stock rights. Acceptance: None.
- Metadata relevance: {"score": null, "confidence": "generated", "scene_matches": null, "subject_matches": null, "query_provenance": null, "selection_tier": null}
- OpenCLIP: {"status": "verified", "accepted": true, "verified": true, "score": 0.2780808165669441, "scene_score": 0.27949684858322144, "presentation_risk": false, "threshold": 0.24}
- Acceptance: null; source `generated_image`; asset status `generated_image_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "GENERATE_FALLBACK", "resolved_type": "generated_image", "decision_reason": "no_accepted_real_media:budget_exhausted:none", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}


### Warum öffnen wir den Kühlschrank, obwohl wir keinen Hunger haben?

Project: `60d597e0-d6ae-4259-b1e9-23b25508d593`. Created 2026-10-02T12:13:47.377039+00:00; export 2026-10-02T12:31:41.495062+00:00.

#### scene_06_01 — 20.3–23.5 s

Assessment: **WRONG — fashion/twirling subject has no behavioral-mechanism relationship**.

- Narration: Dann locken nicht nur Lebensmittel, sondern auch Neugier, Trost
- Visual intent: {"visual_goal": "Dann locken nicht Lebensmittel sondern auch", "objects": ["Dann", "locken", "nicht"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `locken lebensmittel nur neugier trost`. Planned queries: ["locken lebensmittel nur neugier trost", "öffnen den kühlschrank obwohl"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `locken lebensmittel nur neugier trost` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=5, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=partial; `öffnen den kühlschrank obwohl` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=0, rights-rejects=0, relevance-rejects=0]; coverage=partial
- Winning provider / ID: `pexels` / `32152258`.
- Source: https://www.pexels.com/video/elegant-woman-with-curly-hair-twirling-32152258/
- Candidate title:
- Description excerpt:
- Tags: []
- Rights: {"license_id": "pexels-license", "public_domain": null, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "provider_terms:https://www.pexels.com/license/", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": -60.0, "confidence": "acceptable", "scene_matches": [], "subject_matches": [], "query_provenance": true, "selection_tier": 1}
- OpenCLIP: {"status": "verified", "score": 0.23979236148297783, "subject_score": 0.20183387398719788, "scene_score": 0.24649091809988022, "provenance": "provider_video_frames", "frame_scores": [0.22841841876506805, 0.23979236148297783, 0.2653376840054989], "frame_count": 3, "presentation_score": 0.13618072494864464, "photographic_score": 0.22252852469682693, "diagram_score": 0.12382446229457855, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "visual_verified", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `video_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_video", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}

#### scene_06_02 — 23.5–26.367 s

Assessment: **WRONG — 1910 newspaper has no relationship to immediate agency/comfort**.

- Narration: und das Gefühl, sofort etwas tun zu können.
- Visual intent: {"visual_goal": "Gefühl sofort etwas können", "objects": ["Gefühl", "sofort", "etwas"], "actions": [], "context": [], "source": "narration_fallback"}
- Query: `gefühl sofort etwas können`. Planned queries: ["gefühl sofort etwas können", "gefühl sofort etwas können tun", "öffnen den kühlschrank obwohl"].
- Routed sources (unique): pexels (video: everyday_action_or_general_photo), openverse (photo: everyday_action_or_general_photo), pexels (photo: alternate_media_kind), wikimedia (photo: everyday_action_or_general_photo)
- Performed work: `gefühl sofort etwas können` [pexels/video: calls=1, returned=18, rights-rejects=0, relevance-rejects=6, openverse/photo: calls=0, returned=0, rights-rejects=0, relevance-rejects=0, failure=rate_limited, pexels/photo: calls=1, returned=15, rights-rejects=0, relevance-rejects=15, wikimedia/photo: calls=1, returned=2, rights-rejects=1, relevance-rejects=0]; coverage=strong
- Winning provider / ID: `wikimedia` / `152195240`.
- Source: https://commons.wikimedia.org/wiki/File:Zwischen_Antung_und_Mukden._M%C3%BCnchner_neueste_Nachrichten._Handels-Zeitung,_Alpine_und_Sport-Zeitung,_Theater-_und_Kunst-Chronik._20._M%C3%A4rz_1910._S._1_und_2_(M%C3%BCnchner_Digitalisierungzentrum_v2_bsb00130859_00371).jpg
- Candidate title: File:Zwischen Antung und Mukden. Münchner neueste Nachrichten. Handels-Zeitung, Alpine und Sport-Zeitung, Theater- und Kunst-Chronik. 20. März 1910. S. 1 und 2 (Münchner Digitalisierungzentrum v2 bsb00130859 00371).jpg
- Description excerpt: Zwischen Antung und Mukden. Münchner neueste Nachrichten. Handels-Zeitung, Alpine und Sport-Zeitung, Theater- und Kunst-Chronik. 20. März 1910. S. 1 und 2 (Münchner Digitalisierungzentrum v2 bsb00130859 00371) Zwischen Antung und Mukden [ 1 ] Am 22. Septemb
- Tags: []
- Rights: {"license_id": "pd", "public_domain": true, "commercial_use_allowed": true, "modifications_allowed": true, "attribution_required": false, "rights_source": "wikimedia_extmetadata:https://commons.wikimedia.org/wiki/File:Zwischen_Antung_und_Mukden._M%C3%BCnchner_neueste_Nachrichten._Handels-Zeitung,_Alpine_und_Sport-Zeitung,_Theater-_und_Kunst-Chronik._20._M%C3%A4rz_1910._S._1_und_2_(M%C3%BCnchner_Digitalisierungzentrum_v2_bsb00130859_00371).jpg", "rights_policy_version": "clipforge-commercial-edited-v1"}. Acceptance: {'status': 'usable', 'reason': 'established_reuse_rights', 'policy_version': 'clipforge-commercial-edited-v1'}.
- Metadata relevance: {"score": 150.0, "confidence": "high", "scene_matches": ["etwa", "gefühl", "können", "sofort", "tun"], "subject_matches": [], "query_provenance": true, "selection_tier": 3}
- OpenCLIP: {"status": "unavailable_preview", "score": null, "subject_score": null, "scene_score": null, "provenance": null, "frame_scores": [], "frame_count": 0, "presentation_score": null, "photographic_score": null, "diagram_score": null, "presentation_risk": false}
- Acceptance: {"accepted": true, "reason": "metadata_match", "authority": "real_media_quality_gate"}; source `staged_search`; asset status `photo_ready`.
- Director / reuse: {"planned_type": "stock_video", "decision": "ACCEPTED_REAL", "resolved_type": "stock_photo", "decision_reason": "real_media_staged_search", "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}


## Source-specific conclusions

**Mars:** NASA was not routed or queried in any scene. There are no NASA candidates to say were rejected or lost. The old route classified every stage as everyday/general photo. Hook queries were dust/soil rather than astronomy terms; the router did not use the positive primary-answer claim describing an atmosphere. Stock/Openverse were primary; Commons widened only after insufficient coverage. Street selection was `relaxed_fallback` for `kennst rost`, scene score 0.2430499494, combined 0.2323931307, subject 0.1692064106, no metadata score above -60. Relaxation used the same 0.24 threshold; it was not lowered. The error was missing descriptive source evidence and poor fragment context, not a suitability score bonus.

**Berlin:** LOC was attempted for the first hook and explanation scenes and failed with category `invalid_credentials` (anonymous source; could be access/auth HTTP rejection—the original HTTP status was not persisted). Europeana was not enabled in the routes. Openverse was repeatedly rate limited. Commons was queried and returned useful-looking pools but some Wall-query results were rights-rejected; these rejects remain authoritative. Current Pexels location imagery and unrelated Commons documents subsequently passed. The snowy-city repeat in scene_05_02 was an actual Final Critic base repair with `final_critic_destination_frames`, score 0.2696779668. The foundation destination gate participated, but unknown metadata plus a passing scene scalar and no temporal constraint allowed it.

**Fridge:** neither LOC nor Europeana was routed. Commons was a permitted general-photo widening source, not an archival-provider leak. Its 1910 newspaper won after stock candidates lacked coverage and Openverse was rate limited. Long OCR text matched `etwa, gefühl, können, sofort, tun`, score 150; preview unavailable; `metadata_match` accepted it. The Pexels fashion video was fresh staged selection, scene 0.2464909181, subject 0.2018338740, empty metadata score -60. Saved render times are 20.30–23.50 / 23.50–26.367 seconds, not the approximate user timings. Retained frames confirm those identities.

**Kaugummi:** only one real Pexels mouth video and two generated-image identities appear in the rendered layout. Both generated descriptions explicitly ask for gum stretching/folding between teeth or pulled from the mouth; the final two scenes use the same generated identity for one block. The selected facts/script contain no distinct material-manufacturing or heat/moisture beat; the latter research fact was off-question/omittable. This is chiefly repeated planned concepts and fitting same-block continuity, not repeated bad stock or cross-source duplication. Retrieval does not invent new concepts to force diversity.

## Limits and real retest

The fix was replayed against saved real candidates and tested with generic mocked pools. Existing real project records/videos were not edited, regenerated or relabeled. Current exported projects lack asset caches by deliberate cleanup; simply reopening them cannot validate a new acquisition. Generate NEW projects for all four exact questions on the feature branch. Inspect the new diagnostics for NASA routing, historical date evidence, rejected source-caption/long-OCR mismatches, budgets and completed permitted fallbacks. An asset with no sufficient real evidence must fall back; a repeated fitting visual remains preferable to unrelated diversity.

## Validation

- 29 new generic regression cases passed.
- Focused provider/router/media/reuse/director/verifier/Critic tests: 537 passed.
- All existing semantic-selection and fallback-completion (Kaugummi mechanism) regressions: 42 passed.
- Full backend: 1880 passed (172 existing dependency/migration warnings).
- Ruff and `git diff --check`: passed.
- Frontend unchanged; no frontend tests required for this backend-only fix.
- Real saved-candidate replay rejects the reported street, fashion video, newspaper, unrelated history documents and modern-city critic reuse while retaining the fitting mouth visual. No paid generation or live provider calls were used for validation.
