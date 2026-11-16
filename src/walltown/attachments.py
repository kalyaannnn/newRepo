"""Email attachment references are evidence, not inferred attachment content."""

from __future__ import annotations

import csv
import re
from pathlib import PurePosixPath

from walltown.extraction_models import AttachmentReference, ExtractedDocument


ATTACHMENT_HEADER = re.compile(r"^\s*(?:attachments?|attached\s+files?)(?:\s*[:：]\s*|\s*$)", re.IGNORECASE)
OTHER_HEADER = re.compile(r"^\s*(?:from|to|cc|bcc|subject|sent|date|importance|sensitivity)\s*:", re.IGNORECASE)
FILE_END = re.compile(r"\.(?:pdf|docx?|xlsx?|xlsm|csv|tsv|txt|rtf|pptx?|jpe?g|png|gif|tiff?|bmp|msg|eml|zip|7z|rar|html?|xml|dwg|vsdx?|ics|wav|mp3|mp4|dat|bin)\b", re.IGNORECASE)


def filename_key(value: str) -> str:
    """An exact basename comparison is an availability hint, never identity."""
    return PurePosixPath(value.replace("\\", "/")).name.casefold()


def _names(value: str) -> list[str]:
    names: list[str] = []
    for line in value.splitlines():
        delimiter = ";" if ";" in line else "|" if "|" in line else ","
        parts = next(csv.reader([line], delimiter=delimiter, skipinitialspace=True))
        # Keep an unquoted comma in a filename if splitting leaves fragments
        # that cannot independently be filenames.
        if delimiter == "," and len(parts) > 1 and not all(FILE_END.search(part) for part in parts):
            parts = [line]
        for part in parts:
            match = list(FILE_END.finditer(part))
            if not match:
                continue
            candidate = part[:match[-1].end()].strip().strip('"\'<>')
            candidate = re.sub(r"^(?:\d+[.)]\s+|[-•]\s+)", "", candidate)
            candidate = re.sub(r"^\(\d+\)\s+", "", candidate)
            if candidate and candidate not in names:
                names.append(candidate)
    return names


def detect_attachment_references(text: str, page_number: int) -> list[AttachmentReference]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    references: list[AttachmentReference] = []
    for index, line in enumerate(lines):
        match = ATTACHMENT_HEADER.match(line)
        if not match:
            continue
        end_index = index + 1
        values = [line[match.end():].rstrip("\r\n")]
        # Header labels and filenames can occupy separate lines in PDF printouts.
        while end_index < min(index + 16, len(lines)):
            following = lines[end_index]
            if OTHER_HEADER.match(following) or ATTACHMENT_HEADER.match(following) or not following.strip():
                break
            if not FILE_END.search(following):
                break
            values.append(following.rstrip("\r\n"))
            end_index += 1
        start = offsets[index]
        end = offsets[end_index] if end_index < len(lines) else len(text)
        raw_header = text[start:end]
        names = _names("\n".join(values))
        if not names:
            references.append(AttachmentReference(name=None, page_number=page_number, source_start=start,
                              source_end=end, raw_header_text=raw_header, availability="name_unresolved"))
        for name in names:
            references.append(AttachmentReference(name=name, page_number=page_number, source_start=start,
                              source_end=end, raw_header_text=raw_header))
    return references


def resolve_attachment_availability(records: list[ExtractedDocument]) -> None:
    for parent in records:
        for reference in parent.referenced_attachments:
            if reference.name is None:
                parent.extraction_warnings.append("attachment_reference_name_unresolved")
                continue
            key = filename_key(reference.name)
            candidates = [record for record in records if record is not parent and (
                filename_key(record.source_filename) == key or
                (record.original_filename is not None and filename_key(record.original_filename) == key)
            )]
            reference.exported_file_candidates = [record.source_relative_path for record in candidates]
            reference.embedded_attachment_candidates = [index for index, item in enumerate(parent.embedded_attachments)
                if key in {filename_key(value) for value in (item.name, item.filename) if value}]
            reference.matching_basis = "Exact case-insensitive filename candidate only; filename equality does not prove a parent-child relationship."
            if len(candidates) > 1:
                reference.availability = "ambiguous_candidates"
                parent.extraction_warnings.append("attachment_reference_ambiguous")
            elif candidates:
                reference.availability = "exported_candidate"
            elif reference.embedded_attachment_candidates:
                reference.availability = "embedded_candidate"
            else:
                reference.availability = "unavailable"
                parent.extraction_warnings.append("referenced_attachment_unavailable")
            for candidate in candidates:
                if parent.document_id and candidate.document_id and parent.document_id in (candidate.parent_ids or []):
                    reference.relationship_evidence.append("Explicit GoldFynch parent ID on candidate " + candidate.document_id)
            # Do not resolve an ambiguous filename to one of several exports.
            reference.parent_child_relationship_verified = len(candidates) == 1 and bool(reference.relationship_evidence)
        parent.extraction_warnings = list(dict.fromkeys(parent.extraction_warnings))
