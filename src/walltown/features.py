"""Transparent metadata-only review and sampling indicators, never labels."""

from __future__ import annotations

import re

from walltown.manifest import Document, normalize_tag


RESEARCH_TAGS = {
    "LANDFILL KNOWLEDGE", "POSSIBLE PRE-DUKE LEAD KNOWLEDGE",
    "POST-DUKE LEAD KNOWLEDGE", "SOIL TESTING RESULTS", "CONVERSATIONS WITH DEQ",
}
RESEARCH_PATTERN = re.compile(
    r"\b(?:landfill|lead|soil|deq|duke|contaminat\w*|environment\w*|"
    r"water|pollut\w*|toxic\w*|remediat\w*|heavy\s+metals?)\b", re.IGNORECASE,
)
SENSITIVE_PATTERN = re.compile(
    r"\b(?:privileg\w*|confidential\w*|attorney[\s_-]*client|work[\s_-]*product)\b",
    re.IGNORECASE,
)
LARGE_PAGE_COUNT = 200
LARGE_FILE_BYTES = 25 * 1024 * 1024


def discover_research_tags(documents: dict[str, Document], extra: tuple[str, ...] = ()) -> dict[str, str]:
    explicit = {normalize_tag(tag) for tag in (*RESEARCH_TAGS, *extra)}
    return {
        normalize_tag(tag): tag
        for doc in documents.values() for tag in doc.tags
        if normalize_tag(tag) in explicit or RESEARCH_PATTERN.search(tag)
    }


def review_flags(doc: Document) -> list[str]:
    flags: list[str] = []
    for field in ("privileged", "confidential"):
        if any(value.casefold() not in {"no", "false", "0", "none", "public", "not privileged", "non-privileged"}
               for value in doc.strings(field)):
            flags.append("explicit_" + field + "_metadata")
    for field, values in (("tag", doc.tags), ("filename", doc.strings("filename")), ("folder", doc.strings("folder_path"))):
        if any(SENSITIVE_PATTERN.search(value) for value in values):
            flags.append(field + "_sensitivity_keyword")
    return flags


def difficulty_flags(doc: Document) -> list[str]:
    flags: list[str] = []
    if "tags" not in doc.values and not doc.tags:
        flags.append("tag_metadata_unavailable")
    pages, size = doc.number("page_count"), doc.number("file_size_bytes")
    if pages is None:
        flags.append("page_count_missing_invalid_or_conflicting")
    elif pages >= LARGE_PAGE_COUNT:
        flags.append("large_page_count")
    if size is None:
        flags.append("byte_count_missing_invalid_or_conflicting")
    elif size >= LARGE_FILE_BYTES:
        flags.append("large_file_size")
    if not any(doc.hashes.values()):
        flags.append("content_hash_missing_or_invalid")
    types = [value.casefold() for value in doc.strings("file_type")]
    if not types:
        flags.append("file_type_missing")
    if any(value.startswith(("image/", "audio/", "video/")) for value in types):
        flags.append("image_audio_or_video_requires_review")
    if any(any(marker in value for marker in ("zip", "compressed", "archive", "octet-stream", "outlook", "pst", "message/")) for value in types):
        flags.append("container_email_or_unknown_binary")
    if any(value.casefold() not in {"no", "false", "0", "none"} for value in doc.strings("encrypted")):
        flags.append("encryption_or_password_metadata")
    return flags
