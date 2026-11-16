"""Read GoldFynch metadata locally, retaining every original column and value."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


class ManifestValidationError(ValueError):
    """A manifest cannot be interpreted without inventing required metadata."""


ALIASES: dict[str, set[str]] = {
    "document_id": {"gfid", "goldfynchid", "goldfynchdocumentid", "documentid", "docid", "fileid", "itemid", "id"},
    "filename": {"filename", "originalfilename", "nativefilename", "documentname"},
    "folder_path": {"gffolder", "folderpath", "folder", "filepath", "fullpath", "originalpath"},
    "tags": {"tags", "tag", "goldfynchtags", "documenttags"},
    "page_count": {"pagecount", "pages", "numberofpages", "totalpages"},
    "file_size_bytes": {"bytecount", "bytes", "filebytes", "filesizebytes", "sizebytes", "filesizeinbytes"},
    "file_type": {"mimetype", "filetype", "contenttype", "mime"},
    "extension": {"extension", "ext", "fileextension"},
    "md5": {"md5", "md5hash", "md5checksum"},
    "sha1": {"sha1", "sha1hash", "sha1checksum"},
    "sha256": {"sha256", "sha256hash", "sha256checksum"},
    "sha512": {"sha512", "sha512hash", "sha512checksum"},
    "hash": {"hash", "contenthash", "filehash", "checksum"},
    "parent_id": {"parentid", "parentdocumentid", "parentgfid", "parentfileid"},
    "family_id": {"familyid", "documentfamilyid", "familyidentifier"},
    "primary_date": {"primarydate", "documentdate", "date"},
    "created_date": {"createddate", "datecreated", "creationdate", "createdtime"},
    "modified_date": {"modifieddate", "datemodified", "lastmodified", "modificationdate"},
    "sent_date": {"sentdate", "datesent", "senttime"},
    "received_date": {"receiveddate", "datereceived", "receivedtime"},
    "uploaded_date": {"uploadeddate", "uploaddate", "dateuploaded"},
    "redacted": {"redacted", "isredacted", "hasredactions"},
    "privileged": {"privileged", "isprivileged", "privilege", "privilegestatus"},
    "confidential": {"confidential", "isconfidential", "confidentiality", "confidentialitystatus"},
    "encrypted": {"encrypted", "isencrypted", "passwordprotected"},
}
HASH_LENGTHS = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}
HASH_FIELDS = (*HASH_LENGTHS, "hash")
OPTIONAL_FIELDS = tuple(name for name in ALIASES if name != "document_id")


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def normalize_tag(value: str) -> str:
    return " ".join(value.split()).casefold()


def json_value(value: Any) -> Any:
    """Convert Excel scalars to JSON without interpreting missing strings as data."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return json_value(value.item())
    return value


def present(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def text_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_count(value: Any) -> int | None:
    """Accept explicit, nonnegative whole counts; never replace blanks with zero."""
    if not present(value) or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def parse_tags(value: Any) -> list[str]:
    """Support GoldFynch semicolons, JSON arrays, and quoted CSV alternatives."""
    if not present(value):
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if present(item)]
    text = str(value).strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            if isinstance(items, list):
                return [str(item).strip() for item in items if present(item)]
    delimiter = next((separator for separator in (";", "\n", "|", ",") if separator in text), None)
    items = next(csv.reader([text], delimiter=delimiter, skipinitialspace=True)) if delimiter and delimiter != "\n" else text.splitlines()
    return [item.strip() for item in items if item.strip()]


def valid_hash(algorithm: str, value: Any) -> str | None:
    text = text_value(value).lower() if present(value) else ""
    length = HASH_LENGTHS.get(algorithm)
    if (length and len(text) != length) or (not length and not 16 <= len(text) <= 128):
        return None
    return text if re.fullmatch(r"[0-9a-f]+", text) else None


def map_columns(columns: list[str], overrides: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    reverse = {alias: canonical for canonical, aliases in ALIASES.items() for alias in aliases}
    for column in columns:
        canonical = reverse.get(normalize_header(column))
        if canonical:
            result.setdefault(canonical, []).append(column)
    if overrides:
        if any(key not in ALIASES for key in overrides):
            raise ManifestValidationError("Column mapping contains an unsupported canonical field.")
        if any(value not in columns for value in overrides.values()):
            raise ManifestValidationError("A configured source column is absent from the selected worksheet.")
        if len(set(overrides.values())) != len(overrides):
            raise ManifestValidationError("One source column cannot map to several canonical fields.")
        overridden_sources = set(overrides.values())
        result = {key: [col for col in sources if col not in overridden_sources] for key, sources in result.items()}
        result = {key: sources for key, sources in result.items() if sources}
        result.update({key: [value] for key, value in overrides.items()})
    return result


@dataclass
class Document:
    document_id: str
    values: dict[str, list[Any]] = field(default_factory=dict)
    original_metadata: list[dict[str, Any]] = field(default_factory=list)
    source_rows: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def strings(self, name: str) -> list[str]:
        return list(dict.fromkeys(text_value(value) for value in self.values.get(name, []) if present(value)))

    def first(self, name: str) -> str | None:
        values = self.strings(name)
        return values[0] if values else None

    def number(self, name: str) -> int | None:
        values = {number for value in self.values.get(name, []) if (number := parse_count(value)) is not None}
        return next(iter(values)) if len(values) == 1 else None

    @property
    def normalized_tags(self) -> set[str]:
        return {normalize_tag(tag) for tag in self.tags}

    @property
    def hashes(self) -> dict[str, list[str]]:
        return {
            name: sorted({hashed for value in self.values.get(name, []) if (hashed := valid_hash(name, value))})
            for name in HASH_FIELDS if self.values.get(name)
        }


@dataclass
class Manifest:
    source_path: Path
    source_sha256: str
    worksheet: str
    worksheet_count: int
    columns: list[str]
    column_mapping: dict[str, list[str]]
    documents: dict[str, Document]
    record_count: int
    missing_id_records: int
    missing_values: dict[str, int]
    invalid_numeric_values: dict[str, int]
    invalid_hash_values: dict[str, int]
    numeric_id_records: int

    @property
    def missing_optional_columns(self) -> list[str]:
        return sorted(set(OPTIONAL_FIELDS) - self.column_mapping.keys())


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def read_manifest(path: Path, *, sheet: str | int | None = None, column_map: Mapping[str, str] | None = None) -> Manifest:
    if not path.is_file():
        raise ManifestValidationError("Manifest is required at data/raw/wallTownManifest.xlsx, or supply --manifest.")
    try:
        source_hash = file_sha256(path)
        with pd.ExcelFile(path, engine="openpyxl") as book:
            candidates: list[tuple[str, dict[str, list[str]]]] = []
            names = [book.sheet_names[sheet]] if isinstance(sheet, int) else [sheet] if sheet is not None else book.sheet_names
            for name in names:
                if name not in book.sheet_names:
                    raise ManifestValidationError("The requested worksheet does not exist.")
                columns = [str(value) for value in pd.read_excel(book, sheet_name=name, nrows=0).columns]
                # Mapping overrides may belong to the document sheet while other
                # worksheets contain only summary tables.
                if sheet is None and column_map and any(source not in columns for source in column_map.values()):
                    continue
                mapping = map_columns(columns, column_map)
                if "document_id" in mapping:
                    candidates.append((name, mapping))
            if not candidates:
                raise ManifestValidationError("Missing required column: document_id. Use --column-map for an unrecognized ID header; IDs will not be invented.")
            if len(candidates) > 1:
                raise ManifestValidationError("Multiple document worksheets found. Select one explicitly using --sheet.")
            name, mapping = candidates[0]
            frame = pd.read_excel(book, sheet_name=name, dtype=object, keep_default_na=False)
            worksheet_count = len(book.sheet_names)
    except ManifestValidationError:
        raise
    except Exception:
        raise ManifestValidationError("Cannot read the Excel manifest with openpyxl. Check the workbook format and local file permissions.") from None

    documents: dict[str, Document] = {}
    missing = {canonical: 0 for canonical in ALIASES}
    invalid_counts = {name: 0 for name in ("page_count", "file_size_bytes")}
    invalid_hashes = {name: 0 for name in HASH_FIELDS}
    missing_ids = numeric_ids = 0
    for row_number, row in enumerate(frame.to_dict(orient="records"), start=2):
        original = {str(key): json_value(value) for key, value in row.items()}
        values = {
            canonical: [original[source] for source in sources if present(original[source])]
            for canonical, sources in mapping.items()
        }
        for canonical in ALIASES:
            missing[canonical] += not bool(values.get(canonical))
        for canonical in invalid_counts:
            invalid_counts[canonical] += sum(parse_count(value) is None for value in values.get(canonical, []))
        for canonical in HASH_FIELDS:
            invalid_hashes[canonical] += sum(valid_hash(canonical, value) is None for value in values.get(canonical, []))
        ids = {text_value(value) for value in values.get("document_id", [])}
        if len(ids) > 1:
            raise ManifestValidationError(f"Conflicting document IDs in Excel row {row_number}. Resolve the ID column mapping.")
        if not ids:
            missing_ids += 1
            continue
        numeric_ids += any(isinstance(value, (int, float)) for value in values["document_id"])
        identifier = next(iter(ids))
        doc = documents.setdefault(identifier, Document(identifier))
        doc.source_rows.append(row_number)
        doc.original_metadata.append(original)
        for canonical, items in values.items():
            target = doc.values.setdefault(canonical, [])
            for value in items:
                if value not in target:
                    target.append(value)
        for value in values.get("tags", []):
            for tag in parse_tags(value):
                if tag not in doc.tags:
                    doc.tags.append(tag)
    if not documents:
        raise ManifestValidationError("No nonempty document IDs found. Add real IDs before pilot sampling.")
    if file_sha256(path) != source_hash:
        raise ManifestValidationError("The source workbook changed while being read. Run again on a stable local copy.")
    return Manifest(path, source_hash, name, worksheet_count, [str(col) for col in frame.columns], mapping,
                    documents, len(frame), missing_ids, missing, invalid_counts, invalid_hashes, numeric_ids)
