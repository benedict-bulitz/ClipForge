from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

STRUCTURAL_LABELS = {
    "answer",
    "cause",
    "context",
    "detail",
    "hook",
    "intro",
    "outro",
    "payoff",
    "setup",
    "support",
    "turn",
}

BOILERPLATE_OPENINGS = (
    "the short answer to",
    "the answer starts here",
    "today we're going to",
    "today we are going to",
    "have you ever wondered",
    "in this video",
    "let's dive in",
    "let us dive in",
    "let's take a look",
    "let us take a look",
    "before we answer",
    "die kurze antwort auf",
    "die antwort beginnt hier",
    "heute erklären wir",
    "hast du dich jemals gefragt",
    "schauen wir uns an",
    "bevor wir das beantworten",
)

_TRUNCATED_LEAD_INS = (
    "although",
    "because",
    "while",
    "whereas",
    "according to",
    "obwohl",
    "weil",
    "während",
    "waehrend",
    "laut",
)

_TRUNCATED_ENDINGS = {
    "about",
    "almost",
    "approximately",
    "around",
    "by",
    "less",
    "more",
    "nearly",
    "of",
    "over",
    "than",
    "to",
    "under",
    "with",
    "etwa",
    "fast",
    "knapp",
    "mehr",
    "ungefähr",
    "ungefaehr",
    "weniger",
}


class _NarrationHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.suppressed += 1
        elif tag in {"br", "p", "div", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.suppressed:
            self.suppressed -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def clean_narration_text(value: object) -> str:
    """Return plain, speakable text without presentation or scraper artifacts."""
    text = html.unescape(str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"</?[a-zA-Z][^>]*>", text):
        parser = _NarrationHTMLParser()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)

    text = re.sub(r"!\[([^]]*)]\([^)]*\)", r"\1", text)
    text = re.sub(
        r"\[([^]]+)]\((?:https?://|www\.)[^)]*\)",
        _markdown_link_text,
        text,
    )
    text = re.sub(r"(?i)\[(?:\d{1,3}|citation needed)]", "", text)
    text = re.sub(
        r"\([A-Z][A-Za-z&.\s-]{1,60},\s*(?:19|20)\d{2}[a-z]?\)",
        "",
        text,
    )
    text = re.sub(r"(?:https?://|www\.)\S+", "", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"(?m)^\s*(?:[-+*]|\d+[.)])\s+", "", text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__|~~([^~]+)~~", _first_group, text)
    text = re.sub(r"(?<!\w)[*_`]([^*_`]+)[*_`](?!\w)", r"\1", text)

    cleaned_lines: list[str] = []
    labels = "|".join(sorted(STRUCTURAL_LABELS, key=len, reverse=True))
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Search snippets often surround an injected editor marker with truncation marks. A normal
        # sentence about an editor remains valid narration.
        if re.search(
            r"(?i)(?:\.{3}|…)\s*editor\b|\beditor\b[\s.:-]{0,8}(?:\.{3}|…)",
            line,
        ):
            continue
        if re.search(r"(?i)^\s*(?:sources?|citations?|references?|quellen?|belege?)\s*:", line):
            continue
        if re.fullmatch(r"(?i)\s*(?:sources?|citations?|references?|quellen?|belege?)\s*", line):
            continue
        if re.search(r"(?:\.{3}|…)\s*$", line):
            without_ellipsis = re.sub(r"(?:\.{3}|…)\s*$", "", line).rstrip()
            final_word = re.findall(r"[A-Za-zÄÖÜäöüß]+", without_ellipsis.casefold())
            if (
                not without_ellipsis
                or without_ellipsis.casefold().startswith(_TRUNCATED_LEAD_INS)
                or (final_word and final_word[-1] in _TRUNCATED_ENDINGS)
            ):
                continue
            line = without_ellipsis + "."
        if re.fullmatch(rf"(?i)(?:{labels})\s*:?[.!-]?", line):
            if cleaned_lines and cleaned_lines[-1][-1:] not in ".!?…\"'”’":
                cleaned_lines[-1] += "."
            continue
        line = re.sub(rf"(?i)^(?:{labels})\s*[:—-]\s*", "", line)
        if re.match(
            r"(?i)^(?:system|assistant|model|developer|clipforge(?:\s+instruction)?)\s*:",
            line,
        ):
            continue
        cleaned_lines.append(line)
    text = " ".join(cleaned_lines)

    text = re.sub(
        r"(?i)\s*[;,]?\s*(?:please\s+)?(?:add|include|insert|attach)\s+(?:the\s+)?"
        r"(?:original\s+)?(?:source|citation|reference)\s+links?\s+"
        r"(?:before|prior\s+to)\s+publication\b[.!]?",
        "",
        text,
    )
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence.strip()
    ]
    text = " ".join(
        sentence
        for sentence in sentences
        if not re.search(
            r"(?i)^(?:that|this|the)\s+(?:assessment|answer|claim|summary)\s+is\s+"
            r"based\s+on\s+(?:the\s+)?(?:provided|supplied)\s+"
            r"(?:material|sources?|research)\b",
            sentence,
        )
    )

    text = re.sub(
        r"(?i)^the short answer to\s+['\"“][^'\"”]{1,180}['\"”]\s*:\s*",
        "",
        text,
    )
    text = re.sub(r"(?i)^the short answer to\s+.{1,180}?\s+starts here\s*[:—-]\s*", "", text)
    text = re.sub(r"(?i)^the short answer to\s+.{1,180}\s+is\s*[:—-]\s*", "", text)
    text = re.sub(r"(?i)^the short answer is\s*[:—-]?\s*", "", text)
    text = re.sub(
        r"(?i)^die kurze antwort auf\s+.{1,180}?(?:beginnt hier|ist)\s*[:—-]\s*",
        "",
        text,
    )
    text = re.sub(r"(?i)^die kurze antwort ist\s*[:—-]?\s*", "", text)
    text = re.sub(
        r"(?i)^(?:in this video|today (?:we(?:'re| are) going to)|let(?:'s| us) dive in|"
        r"before we answer that|have you ever wondered)[^.!?]*[.!?]\s*",
        "",
        text,
    )

    text = re.sub(
        r"(?:\.{3}|…)+\s*(?:editor|read more|more|weiterlesen)\.?\s*(?:\.{3}|…)+",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?:\.{3}|…)+", ". ", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])(?=[A-Za-zÄÖÜäöüß])", r"\1 ", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\n-–—")
    if text and text[-1] not in ".!?…\"'”’":
        text += "."
    return text


def clean_research_claim(value: object) -> str:
    """Reduce a search snippet to useful evidence without spoken attribution scaffolding."""
    text = clean_narration_text(value)
    text = re.sub(
        r"(?i)^(?:according to|as reported by|source:?|quelle:?|laut)\s+[^,;:]{2,90}[,;:]\s*",
        "",
        text,
    )
    text = re.sub(
        r"(?i)^(?:[A-ZÄÖÜ][\wÄÖÜäöüß&.-]{1,80}\s+"
        r"(?:erklärt|zeigt|berichtet|beschreibt|erläutert|sagt|"
        r"explains|shows|reports|describes|says))\s*,?\s*"
        r"(?:warum|wieso|weshalb|how|why|that|dass)?\s*",
        "",
        text,
    )
    text = re.sub(
        r"(?i)^(?:die redaktion|the editorial team|the article|dieser artikel|"
        r"this article)\s+(?:erklärt|zeigt|berichtet|beschreibt|explains|"
        r"shows|reports|describes)\s*(?:,|:)?\s*",
        "",
        text,
    )
    text = re.sub(
        r"(?i)^(?:here(?:'s| is)|hier ist|in diesem artikel|in this article|"
        r"eines vorweg|first of all)\s*[:,-]?\s*",
        "",
        text,
    )
    if re.search(r"(?i)\beditor\b|(?:\.{3}|…)", str(value or "")):
        text = re.sub(r"(?i)\beditor\b\.?", "", text)
    return re.sub(r"\s+", " ", text).strip()


def contamination_issues(value: object) -> list[str]:
    raw = str(value or "")
    text = raw
    issues: list[str] = []
    if re.search(r"</?[a-zA-Z][^>]*>", text):
        issues.append("raw HTML")
    if re.search(
        r"(?m)^\s{0,3}#{1,6}\s+|```|\*\*[^*]+\*\*|__[^_]+__|"
        r"~~[^~]+~~|\[[^]]+]\((?:https?://|www\.)",
        text,
    ):
        issues.append("Markdown presentation markup")
    labels = "|".join(sorted(STRUCTURAL_LABELS, key=len, reverse=True))
    if re.search(rf"(?i)^\s*(?:{labels})\s*[:—-]", raw):
        issues.append("structural label")
    if re.search(rf"(?im)^\s*(?:{labels})\s*:?[.!-]?\s*$", text):
        issues.append("standalone structural label")
    if re.search(
        r"(?i)(?:\.{3}|…)\s*editor\b|\beditor\b[\s.:-]{0,8}(?:\.{3}|…)",
        text,
    ):
        issues.append("search-result editor artifact")
    if re.search(r"(?:https?://|www\.)\S+", text):
        issues.append("URL")
    if re.search(
        r"(?im)^\s*(?:sources?|citations?|references?|quellen?|belege?)\s*:|"
        r"\[(?:\d{1,3}|citation needed)]|"
        r"\([A-Z][A-Za-z&.\s-]{1,60},\s*(?:19|20)\d{2}[a-z]?\)",
        text,
    ):
        issues.append("citation or source fragment")
    if re.search(
        r"\b(?:system prompt|hidden instruction|as an ai|language model|"
        r"clipforge instruction|internal clipforge)\b|"
        r"^\s*(?:system|assistant|model|developer|clipforge)\s*:",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    ):
        issues.append("model or internal meta commentary")
    if re.search(
        r"(?i)(?:add|include|insert|attach)\s+(?:the\s+)?(?:original\s+)?"
        r"(?:source|citation|reference)\s+links?\s+(?:before|prior\s+to)\s+publication|"
        r"(?:assessment|answer|claim|summary)\s+is\s+based\s+on\s+"
        r"(?:the\s+)?(?:provided|supplied)\s+(?:material|sources?|research)",
        text,
    ):
        issues.append("editorial instruction")
    if re.search(
        r"(?:[A-ZÄÖÜ][A-ZÄÖÜ0-9&.-]{2,80}\s+"
        r"(?i:erklärt|zeigt|berichtet|beschreibt|erläutert|sagt|"
        r"explains|shows|reports|describes|says)|"
        r"(?i:die redaktion|the editorial team|the article|dieser artikel|"
        r"this article)\s+)",
        text,
    ):
        issues.append("publisher or editorial boilerplate")
    if re.search(r"(?:\.{3}|…){1,}", text):
        issues.append("truncated snippet marker")
    return issues


def begins_with_preamble(value: object) -> bool:
    text = re.sub(r"^[#>*_`\s]+", "", html.unescape(str(value or ""))).casefold()
    return any(text.startswith(opening) for opening in BOILERPLATE_OPENINGS)


def clean_script_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for block in blocks:
        role = str(block.get("role") or "detail").strip().casefold() or "detail"
        text = clean_narration_text(block.get("text"))
        if any(
            issue == "publisher or editorial boilerplate"
            for issue in contamination_issues(text)
        ):
            continue
        if text:
            cleaned = {"role": role, "text": text}
            fact_ids = block.get("fact_ids")
            if isinstance(fact_ids, list):
                cleaned["fact_ids"] = [str(fact_id) for fact_id in fact_ids if str(fact_id).strip()]
            clean.append(cleaned)
    return clean


def _first_group(match: re.Match[str]) -> str:
    return next((group for group in match.groups() if group is not None), "")


def _markdown_link_text(match: re.Match[str]) -> str:
    label = match.group(1).strip()
    if re.fullmatch(r"(?i)(?:source|citation|reference|read more|quelle|weiterlesen)", label):
        return ""
    return label
