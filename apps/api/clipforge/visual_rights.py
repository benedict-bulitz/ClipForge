"""Small copyright reuse authority. None means unknown, never permission.

This checks evidence for edited commercial video, not model/property releases.
Provider-wide grants are explicitly identified; legacy assets are not upgraded.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

RIGHTS_POLICY_VERSION = "clipforge-commercial-edited-v1"


@dataclass(frozen=True)
class MediaRights:
    license_id: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    public_domain: bool | None = None
    commercial_use_allowed: bool | None = None
    modifications_allowed: bool | None = None
    attribution_required: bool | None = None
    attribution_text: str | None = None
    rights_source: str | None = None
    rights_policy_version: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def serialize(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def read(cls, value: object) -> MediaRights:
        if isinstance(value, cls):
            value = value.serialize()
        if not isinstance(value, dict):
            return cls()
        fields = {}
        for name in cls.__dataclass_fields__:
            item = value.get(name)
            if name in {
                "public_domain",
                "commercial_use_allowed",
                "modifications_allowed",
                "attribution_required",
            }:
                fields[name] = item if type(item) is bool else None
            elif name == "evidence":
                fields[name] = item if isinstance(item, dict) else {}
            else:
                fields[name] = item.strip() if isinstance(item, str) and item.strip() else None
        return cls(**fields)


@dataclass(frozen=True)
class RightsDecision:
    status: Literal["usable", "unusable", "unknown"]
    reason: str
    policy_version: str = RIGHTS_POLICY_VERSION


def evaluate_rights(value: object, *, modifies: bool = True) -> RightsDecision:
    rights = MediaRights.read(value)
    if rights.evidence.get("unresolved_restrictions") or rights.evidence.get(
        "conflicting_license_metadata"
    ):
        return RightsDecision("unknown", "ambiguous_license_evidence")
    if rights.commercial_use_allowed is False:
        return RightsDecision("unusable", "commercial_use_denied")
    if modifies and rights.modifications_allowed is False:
        return RightsDecision("unusable", "modifications_denied")
    # Share-alike/other obligations are not fulfilled by merely keeping a credit.
    if rights.license_id and (
        "-sa" in rights.license_id.casefold() or "gfdl" in rights.license_id.casefold()
    ):
        return RightsDecision("unusable", "unsupported_license_obligations")
    if not rights.rights_source or not (rights.license_id or rights.license_url):
        return RightsDecision("unknown", "missing_license_evidence")
    if rights.public_domain is not True and rights.commercial_use_allowed is not True:
        return RightsDecision("unknown", "commercial_use_unknown")
    if modifies and rights.public_domain is not True and rights.modifications_allowed is not True:
        return RightsDecision("unknown", "modifications_unknown")
    if rights.attribution_required is True and not rights.attribution_text:
        return RightsDecision("unknown", "attribution_missing")
    if rights.attribution_required is None and rights.public_domain is not True:
        return RightsDecision("unknown", "attribution_unknown")
    return RightsDecision("usable", "established_reuse_rights")


def accepted_rights(value: object) -> dict[str, Any]:
    rights = MediaRights.read(value).serialize()
    decision = evaluate_rights(value)
    if decision.status != "usable":
        raise ValueError(decision.reason)
    rights["rights_policy_version"] = decision.policy_version
    return rights


def provider_terms_rights(provider: str) -> MediaRights:
    """Only called by API result normalizers, never by cache readers.

    Pixabay API does not establish publication date/CC0. Use the current
    provider-wide reuse grant, without claiming public-domain status.
    """
    terms = {
        "pexels": ("Pexels License", "https://www.pexels.com/license/"),
        "pixabay": ("Pixabay Content License", "https://pixabay.com/service/terms/"),
    }
    name, url = terms[provider]
    return MediaRights(
        license_id=f"{provider}-license",
        license_name=name,
        license_url=url,
        commercial_use_allowed=True,
        modifications_allowed=True,
        attribution_required=False,
        rights_source=f"provider_terms:{url}",
        evidence={
            "basis": "provider-wide grant; not per-result API metadata",
            "reviewed_on": "2026-10-01",
            "scope": "edited commercial video; provider prohibited uses and third-party rights still apply",
        },
    )


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def commons_rights(metadata: dict[str, Any], *, source_url: str, creator: str) -> MediaRights:
    def get(key: str) -> str:
        value = metadata.get(key)
        return str(value.get("value") or "").strip() if isinstance(value, dict) else ""

    raw = {
        key: get(key)
        for key in (
            "License",
            "LicenseShortName",
            "LicenseUrl",
            "UsageTerms",
            "AttributionRequired",
            "Attribution",
            "Copyrighted",
            "Restrictions",
            "Credit",
            "Artist",
        )
        if get(key)
    }
    identifier = get("License").lower() or None
    name = _plain(get("LicenseShortName") or get("UsageTerms")) or None
    url = html.unescape(get("LicenseUrl")) or None
    parsed = urlparse(url or "")
    # An exact CC deed path establishes a known license; substring matching
    # would wrongly allow CC BY-NC or a spoofed host.
    cc_path = (
        parsed.path.strip("/").lower()
        if parsed.hostname in {"creativecommons.org", "www.creativecommons.org"}
        else ""
    )
    public_domain = (
        True
        if cc_path in {"publicdomain/zero/1.0", "publicdomain/mark/1.0"}
        or identifier in {"pd", "cc-zero"}
        else None
    )
    cc_by = bool(re.fullmatch(r"licenses/by/(1\.0|2\.0|2\.5|3\.0|4\.0)", cc_path))
    cc_license = re.fullmatch(
        r"licenses/(by(?:-(?:nc|nd|sa)){0,3})/(1\.0|2\.0|2\.5|3\.0|4\.0)", cc_path
    )
    if cc_license and identifier and identifier != f"cc-{cc_license[1]}-{cc_license[2]}":
        raw["conflicting_license_metadata"] = True
    if public_domain is True and get("Copyrighted").lower() in {"true", "1", "yes"}:
        raw["conflicting_license_metadata"] = True
    if cc_license:
        identifier = f"cc-{cc_license[1]}-{cc_license[2]}"
    if public_domain is True:
        commercial, modifications, attribution = True, True, False
    elif cc_by:
        commercial, modifications, attribution = True, True, True
    else:
        commercial = False if cc_license and "-nc" in cc_license[1] else None
        modifications = False if cc_license and "-nd" in cc_license[1] else None
        attribution = None
    flag = get("AttributionRequired").lower()
    if flag in {"true", "1", "yes"}:
        attribution = True
    # Retain explicitly supplied credit and all relevant raw fields. An
    # assembled CC BY credit needs a real Artist plus the page and deed URL.
    credit = _plain(get("Attribution")) or None
    if attribution is True and creator and source_url and url:
        credit = "; ".join(
            filter(
                None,
                [
                    credit,
                    creator,
                    source_url,
                    name or identifier,
                    url,
                    _plain(get("Credit")),
                    "ClipForge: cropped/transformed for video",
                ],
            )
        )
    if get("Restrictions"):
        raw["unresolved_restrictions"] = True
        commercial = None  # unresolved asset-specific restrictions fail closed
    return MediaRights(
        license_id=identifier,
        license_name=name,
        license_url=url,
        public_domain=public_domain,
        commercial_use_allowed=commercial,
        modifications_allowed=modifications,
        attribution_required=attribution,
        attribution_text=credit,
        rights_source=f"wikimedia_extmetadata:{source_url}",
        evidence=raw,
    )
