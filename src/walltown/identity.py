"""Resolve PDF identities using exact hashes or explicit export-to-ID mappings."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd

from walltown.extraction_models import IdentityResolution
from walltown.manifest import Document, ManifestValidationError, json_value, map_columns, normalize_header, parse_tags, present, read_manifest, text_value


EXPORT_HEADERS = {"exportfilename", "exportedfilename", "exportfile", "exportpath", "exportfilepath",
                  "productionfile", "productionfilename", "productionpath", "pdfpath", "pdffilename", "pdffile"}
GOLDFYNCH_ID_HEADERS = {"gfid", "goldfynchid", "goldfynchdocumentid"}


def file_hashes(path: Path) -> dict[str, str]:
    hashers = {name: hashlib.new(name) for name in ("md5", "sha1", "sha256", "sha512")}
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            for hasher in hashers.values():
                hasher.update(block)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def _merge(target: Document, incoming: Document) -> None:
    for name, values in incoming.values.items():
        for value in values:
            if value not in target.values.setdefault(name, []):
                target.values[name].append(value)
    for tag in incoming.tags:
        if tag not in target.tags:
            target.tags.append(tag)
    for metadata in incoming.original_metadata:
        if metadata not in target.original_metadata:
            target.original_metadata.append(metadata)


def _document_from_metadata(row: dict[str, Any], identifier: str) -> Document:
    mapping = map_columns(list(row))
    doc = Document(identifier, original_metadata=[row])
    for canonical, sources in mapping.items():
        doc.values[canonical] = [json_value(row[source]) for source in sources if present(row[source])]
    for value in doc.values.get("tags", []):
        doc.tags.extend(tag for tag in parse_tags(value) if tag not in doc.tags)
    return doc


@dataclass
class IdentityCatalog:
    documents: dict[str, Document] = field(default_factory=dict)
    export_mappings: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    warnings: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    hash_index: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))

    def add_document(self, doc: Document) -> None:
        if doc.document_id in self.documents:
            _merge(self.documents[doc.document_id], doc)
        else:
            self.documents[doc.document_id] = doc

    def index_hashes(self) -> None:
        self.hash_index.clear()
        for identifier, doc in self.documents.items():
            for algorithm, values in doc.hashes.items():
                if algorithm == "hash":
                    continue  # An unspecified algorithm cannot verify a native PDF's identity.
                for value in values:
                    self.hash_index[(algorithm, value)].add(identifier)


def _load_pilot(path: Path, catalog: IdentityCatalog) -> None:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            identifier = row.get("document_id", "").strip()
            if not identifier:
                catalog.warnings.append("pilot_metadata_row_without_document_id")
                continue
            originals = json.loads(row.get("original_metadata") or "[]")
            if not isinstance(originals, list) or any(not isinstance(item, dict) for item in originals):
                raise ValueError("Invalid original metadata structure.")
            if originals:
                for original in originals:
                    catalog.add_document(_document_from_metadata(original, identifier))
                continue
            doc = Document(identifier)
            for name in ("filename", "folder_path"):
                if present(row.get(name)):
                    doc.values[name] = [row[name]]
            for plural, singular in (("parent_ids", "parent_id"), ("family_ids", "family_id")):
                if plural in row:
                    values = json.loads(row[plural] or "[]")
                    if not isinstance(values, list):
                        raise ValueError("Invalid relationship metadata.")
                    doc.values[singular] = values
            if "original_tags" in row:
                doc.tags = parse_tags(row["original_tags"])
                doc.values["tags"] = [row["original_tags"]] if row["original_tags"] else []
            hashes = json.loads(row.get("all_hashes") or "{}")
            if not isinstance(hashes, dict):
                raise ValueError("Invalid hash metadata.")
            for algorithm, values in hashes.items():
                if algorithm in {"md5", "sha1", "sha256", "sha512"} and isinstance(values, list):
                    doc.values[algorithm] = values
            catalog.add_document(doc)


def _metadata_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".csv", ".tsv"}:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ","))
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data = next((data[key] for key in ("documents", "files", "records", "mappings") if isinstance(data.get(key), list)), [])
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("Invalid production metadata structure.")
        return data
    if path.suffix.lower() == ".xlsx":
        with pd.ExcelFile(path, engine="openpyxl") as book:
            return [row for name in book.sheet_names for row in pd.read_excel(book, sheet_name=name, dtype=object, keep_default_na=False).to_dict(orient="records")]
    return []


def _export_key(value: str) -> str | None:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in value:
        return None
    return path.as_posix()  # Exact relative path; never a fuzzy basename join.


def load_identity_catalog(manifest_path: Path | None, pilot_manifest_path: Path | None,
                          production_files: list[Path], export_root: Path) -> IdentityCatalog:
    catalog = IdentityCatalog()
    if manifest_path and manifest_path.is_file():
        try:
            manifest = read_manifest(manifest_path)
        except (ManifestValidationError, OSError):
            catalog.warnings.append("manifest_metadata_load_failed")
        else:
            for doc in manifest.documents.values():
                catalog.add_document(doc)
            catalog.sources.append({"kind": "manifest", "path": str(manifest_path), "document_count": len(manifest.documents), "sha256": manifest.source_sha256})
    else:
        catalog.warnings.append("master_manifest_unavailable")
    if pilot_manifest_path and pilot_manifest_path.is_file():
        try:
            _load_pilot(pilot_manifest_path, catalog)
        except Exception:
            catalog.warnings.append("pilot_metadata_load_failed")
        else:
            catalog.sources.append({"kind": "pilot_manifest", "path": str(pilot_manifest_path)})
    else:
        catalog.warnings.append("pilot_manifest_unavailable")
    for path in production_files:
        if path.suffix.lower() not in {".csv", ".tsv", ".json", ".xlsx"}:
            catalog.warnings.append("production_metadata_preserved_but_format_not_interpreted")
            continue
        try:
            rows = _metadata_rows(path)
            matched = 0
            for row in rows:
                row = {str(key): json_value(value) for key, value in row.items()}
                mapping = map_columns(list(row))
                exports = [key for key in row if normalize_header(key) in EXPORT_HEADERS and present(row[key])]
                ids = {text_value(row[key]) for key in mapping.get("document_id", []) if present(row[key])}
                if not exports or len(ids) != 1:
                    continue
                identifier = next(iter(ids))
                strong_id = any(normalize_header(key) in GOLDFYNCH_ID_HEADERS for key in mapping.get("document_id", []))
                if not strong_id and identifier not in catalog.documents:
                    catalog.warnings.append("production_identifier_not_verified_as_goldfynch_id")
                    continue
                doc = _document_from_metadata(row, identifier)
                catalog.add_document(doc)
                for key in exports:
                    relative = _export_key(text_value(row[key]))
                    if relative is None:
                        catalog.warnings.append("production_export_path_not_relative")
                        continue
                    # Paths may be rooted at the export folder or beside a nested load file.
                    keys = {relative, (PurePosixPath(path.relative_to(export_root).as_posix()).parent / relative).as_posix()}
                    # Native-file hashes can legitimately differ after PDF export.
                    # Only an explicitly named export/PDF hash is a checksum
                    # assertion about the exported PDF itself.
                    export_hashes = {
                        algorithm: [text_value(value).lower()]
                        for header, value in row.items() if present(value)
                        for algorithm in ("md5", "sha1", "sha256", "sha512")
                        if normalize_header(header) in {"export" + algorithm, "exportedfile" + algorithm, "pdf" + algorithm}
                    }
                    for export_key in keys:
                        catalog.export_mappings[export_key].append({"document_id": identifier,
                            "mapping_file": str(path), "export_path_field": key,
                            "declared_hashes": export_hashes})
                    matched += 1
            if not matched:
                catalog.warnings.append("production_metadata_has_no_verified_export_mapping")
            catalog.sources.append({"kind": "production_metadata", "path": str(path), "mapping_count": matched})
        except Exception:
            catalog.warnings.append("production_metadata_load_failed")
    catalog.index_hashes()
    catalog.warnings = list(dict.fromkeys(catalog.warnings))
    return catalog


def resolve_identity(relative_path: str, hashes: dict[str, str], catalog: IdentityCatalog) -> IdentityResolution:
    evidence: list[dict[str, Any]] = []
    constraints: list[set[str]] = []
    for algorithm, value in hashes.items():
        ids = catalog.hash_index.get((algorithm, value), set())
        if ids:
            constraints.append(set(ids))
            evidence.append({"method": "exact_content_hash", "algorithm": algorithm, "document_ids": sorted(ids)})
    mappings = catalog.export_mappings.get(relative_path, [])
    mapping_ids = {item["document_id"] for item in mappings}
    if mapping_ids:
        constraints.append(mapping_ids)
        evidence.extend({"method": "explicit_export_mapping", **item} for item in mappings)
    conflict = any(declared and algorithm in hashes and hashes[algorithm] not in declared
                   for item in mappings for algorithm, declared in item["declared_hashes"].items() if algorithm != "hash")
    all_candidates = set().union(*constraints) if constraints else set()
    candidates = set.intersection(*constraints) if constraints else set()
    if conflict or (constraints and not candidates):
        return IdentityResolution(status="conflict", evidence=evidence, candidate_document_ids=sorted(all_candidates),
                                  warnings=["identity_evidence_conflict", "identity_unresolved"])
    if len(candidates) != 1:
        return IdentityResolution(status="ambiguous" if candidates else "unresolved", evidence=evidence,
                                  candidate_document_ids=sorted(candidates), warnings=(["identity_ambiguous"] if candidates else []) + ["identity_unresolved"])
    identifier = next(iter(candidates))
    doc = catalog.documents[identifier]
    warnings: list[str] = []

    def single(name: str) -> str | None:
        values = doc.strings(name)
        if len(values) > 1:
            warnings.append("identity_metadata_conflict_" + name)
        return values[0] if len(values) == 1 else None

    return IdentityResolution(status="resolved", document_id=identifier, original_filename=single("filename"),
                              source_path=single("folder_path"), original_tags=doc.tags if "tags" in doc.values else None,
                              parent_ids=doc.strings("parent_id") if "parent_id" in doc.values else None,
                              family_ids=doc.strings("family_id") if "family_id" in doc.values else None,
                              original_metadata=doc.original_metadata, evidence=evidence, candidate_document_ids=[identifier], warnings=warnings)
