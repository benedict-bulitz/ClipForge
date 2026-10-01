"""Explicit reusable test asset evidence; never inferred by production code."""

from clipforge.visual_rights import MediaRights

TEST_REUSE_RIGHTS = MediaRights(
    license_id="test-public-domain",
    public_domain=True,
    modifications_allowed=True,
    commercial_use_allowed=True,
    attribution_required=False,
    rights_source="test_fixture:author-dedicated-public-domain",
)
