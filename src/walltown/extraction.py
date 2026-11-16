"""Entirely local PDF extraction with explicit file and page failure records."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterator, TextIO

import pymupdf

from walltown.attachments import detect_attachment_references, resolve_attachment_availability
from walltown.extraction_models import CopyResult, EmbeddedAttachment, ExtractedDocument, ExtractedPage, IdentityResolution
from walltown.identity import IdentityCatalog, file_hashes, load_identity_catalog, resolve_identity
from walltown.manifest import file_sha256, json_value
from walltown.pipeline import _atomic_write, _write_json


class ExtractionValidationError(ValueError):
    """A local precondition failed; messages contain no case metadata."""


PRODUCTION_METADATA_SUFFIXES = {".csv", ".tsv", ".json", ".xlsx", ".xls", ".dat", ".opt", ".lfp", ".txt", ".xml"}


def check_secret_protection(project_root: Path) -> dict[str, Any]:
    path = project_root / ".gitignore"
    rules = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if not {".env", ".venv/", "data/", "__pycache__/"} <= set(rules):
        raise ExtractionValidationError("Required .gitignore protections are missing: .env, .venv/, data/, __pycache__/.")
    try:
        repo = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=project_root, capture_output=True, check=False)
        count = 0
        if repo.returncode == 0:
            tracked = subprocess.run(["git", "ls-files", "-z", "--", ".env", "data/"], cwd=project_root, capture_output=True, check=False)
            if tracked.returncode != 0:
                raise ExtractionValidationError("Could not verify Git secret protection.")
            count = len([entry for entry in tracked.stdout.split(b"\0") if entry])
            if count:
                raise ExtractionValidationError("Protected .env or data files are Git-tracked. Resolve tracking manually; no Git history was changed.")
        return {"git_repository": repo.returncode == 0, "tracked_protected_file_count": count, "required_ignore_rules_present": True}
    except FileNotFoundError:
        raise ExtractionValidationError("Git is unavailable; secret-protection checks could not complete.") from None


def copy_exports(source_dir: Path, destination: Path) -> tuple[list[CopyResult], list[str]]:
    source_dir = source_dir.expanduser().resolve()
    if any(part.is_symlink() for part in (destination.absolute(), *destination.absolute().parents)):
        raise ExtractionValidationError("The copy destination must not contain symlink directories.")
    destination = destination.resolve()
    if not source_dir.is_dir():
        raise ExtractionValidationError("The local pilot export directory is missing.")
    if destination == source_dir or destination.is_relative_to(source_dir) or source_dir.is_relative_to(destination):
        raise ExtractionValidationError("Source and destination export directories must not overlap.")
    paths = sorted(source_dir.rglob("*"), key=lambda path: path.relative_to(source_dir).as_posix())
    results: list[CopyResult] = []
    warnings: list[str] = []
    for source in paths:
        relative = source.relative_to(source_dir)
        if source.is_symlink():
            results.append(CopyResult(relative_path=relative.as_posix(), action="symlink_not_copied", warnings=["source_symlink_not_followed"]))
            continue
        if not source.is_file():
            continue
        target = destination / relative
        result = CopyResult(relative_path=relative.as_posix(), action="copy_failed")
        temporary: Path | None = None
        try:
            result.source_sha256 = file_sha256(source)
            # A pre-existing symlink must never redirect writes outside data/.
            if any(part.is_symlink() for part in (destination, *target.parents)) or target.is_symlink():
                result.warnings.append("destination_symlink_rejected")
                results.append(result)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                result.destination_sha256 = file_sha256(target)
                if result.destination_sha256 != result.source_sha256:
                    result.action = "destination_conflict_not_overwritten"
                    result.warnings.append("existing_copy_hash_mismatch")
                    results.append(result)
                    continue
                result.action = "verified_existing_copy"
            else:
                descriptor, temp_name = tempfile.mkstemp(dir=target.parent)
                os.close(descriptor)
                temporary = Path(temp_name)
                shutil.copy2(source, temporary)
                temporary.chmod(0o600)
                result.destination_sha256 = file_sha256(temporary)
                if result.destination_sha256 != result.source_sha256:
                    raise ValueError("Copy checksum mismatch.")
                if file_sha256(source) != result.source_sha256:
                    raise ValueError("Source changed during copy.")
                os.replace(temporary, target)
                result.action = "copied"
            # Check both final files, including the already-present case.
            if file_sha256(source) != result.source_sha256 or file_sha256(target) != result.source_sha256:
                raise ValueError("Final checksum mismatch.")
            target.chmod(0o600)
            result.verification_status = "verified"
        except Exception:
            result.warnings.append("copy_or_integrity_verification_failed")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        results.append(result)
    if not results:
        warnings.append("source_export_directory_empty")
    return results, warnings


@contextmanager
def quiet_pdf_diagnostics() -> Iterator[None]:
    """MuPDF diagnostics can contain source text; retain only our warning codes."""
    errors = pymupdf.TOOLS.mupdf_display_errors()
    warnings = pymupdf.TOOLS.mupdf_display_warnings()
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            yield
    finally:
        pymupdf.TOOLS.mupdf_display_errors(errors)
        pymupdf.TOOLS.mupdf_display_warnings(warnings)


def _attachment_metadata(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): json_value(item) for key, item in value.items()}


def _extract_page(page: Any, number: int) -> tuple[ExtractedPage, list[EmbeddedAttachment]]:
    warnings: list[str] = []
    attachments: list[EmbeddedAttachment] = []
    try:
        text = page.get_text("text", sort=False)
    except Exception:
        return ExtractedPage(page_number=number, extraction_status="failed", warnings=["page_text_extraction_failed"]), []
    if not isinstance(text, str):
        return ExtractedPage(page_number=number, extraction_status="failed", warnings=["page_text_result_invalid"]), []
    image_count: int | None = None
    try:
        image_count = len(page.get_images(full=True))
    except Exception:
        warnings.append("page_image_inspection_failed")
    visible: bool | None = None
    blank: bool | None = False if text.strip() else None
    ocr: bool | None = False if text.strip() else None
    if not text.strip():
        try:
            # A bounded grayscale render distinguishes white pages from scanned or
            # vector-only content. This does not recognize or alter any text.
            scale = min(1.0, 512 / max(page.rect.width, page.rect.height, 1))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False, annots=True)
            visible = any(value < 255 for value in pixmap.samples)
            blank = not visible
            ocr = visible
        except Exception:
            warnings.append("page_visual_inspection_failed")
            if image_count:
                ocr = True
        warnings.append("blank_page" if blank else "no_extractable_text")
        if ocr:
            warnings.append("ocr_may_be_required")
    elif len(text.strip()) < 40:
        warnings.append("unusually_short_text")
    suspicious = sum(char == "\ufffd" or unicodedata.category(char) in {"Cs", "Co", "Cn"} or
                     (unicodedata.category(char) == "Cc" and char not in "\n\r\t\f") for char in text)
    garbled = bool(text) and (suspicious / len(text) > 0.02 or bool(re.search(r"\(cid:\d+\)", text)))
    if garbled:
        warnings.append("potentially_garbled_text")
    try:
        for annotation in page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT]) or []:
            try:
                info = _attachment_metadata(annotation.file_info)
                attachments.append(EmbeddedAttachment(name=info.get("filename"), filename=info.get("filename"),
                    storage="file_attachment_annotation", page_number=number, metadata=info))
            except Exception:
                attachments.append(EmbeddedAttachment(storage="file_attachment_annotation", page_number=number, inspection_status="failed"))
                warnings.append("embedded_annotation_inspection_failed")
    except Exception:
        warnings.append("page_annotation_inspection_failed")
    return ExtractedPage(page_number=number, text=text, character_count=len(text), is_blank=blank,
                         requires_ocr=ocr, image_count=image_count, has_visible_marks=visible,
                         potentially_garbled=garbled, warnings=warnings), attachments


def failed_document(path: Path, relative_path: str, warning: str, *, sha256: str | None = None,
                    identity: IdentityResolution | None = None) -> ExtractedDocument:
    identity = identity or IdentityResolution(warnings=["identity_unresolved"])
    return ExtractedDocument(source_filename=path.name, source_relative_path=relative_path, local_path=str(path),
        sha256=sha256, identity=identity, document_id=identity.document_id, original_filename=identity.original_filename,
        source_path=identity.source_path, original_tags=identity.original_tags, parent_ids=identity.parent_ids,
        family_ids=identity.family_ids, extraction_status="failed", extraction_warnings=[warning, *identity.warnings])


def extract_pdf(path: Path, *, relative_path: str | None = None, catalog: IdentityCatalog | None = None,
                expected_sha256: str | None = None) -> ExtractedDocument:
    relative = relative_path or path.name
    try:
        hashes = file_hashes(path)
        size = path.stat().st_size
    except Exception:
        return failed_document(path, relative, "source_file_unreadable")
    if expected_sha256 and hashes["sha256"] != expected_sha256:
        return failed_document(path, relative, "copied_pdf_integrity_mismatch", sha256=hashes["sha256"])
    identity = resolve_identity(relative, hashes, catalog or IdentityCatalog())
    warnings = list(identity.warnings)
    pages: list[ExtractedPage] = []
    embedded: list[EmbeddedAttachment] = []
    page_count: int | None = None
    embedded_status = "not_attempted"
    with quiet_pdf_diagnostics():
        try:
            with pymupdf.open(path) as document:
                if not document.is_pdf:
                    raise ValueError("Input is not a PDF.")
                page_count = document.page_count
                if document.needs_pass:
                    warnings.append("pdf_requires_password")
                    pages = [ExtractedPage(page_number=index + 1, extraction_status="failed", warnings=["pdf_requires_password"]) for index in range(page_count)]
                else:
                    if document.is_repaired:
                        warnings.append("pdf_required_parser_repair")
                    embedded_status = "success"
                    try:
                        for name in document.embfile_names():
                            try:
                                info = _attachment_metadata(document.embfile_info(name))
                                embedded.append(EmbeddedAttachment(name=name, filename=info.get("ufilename") or info.get("filename"), storage="embedded_files", metadata=info))
                            except Exception:
                                embedded.append(EmbeddedAttachment(name=name, storage="embedded_files", inspection_status="failed"))
                                embedded_status = "partial"
                                warnings.append("embedded_attachment_metadata_failed")
                    except Exception:
                        embedded_status = "failed"
                        warnings.append("embedded_attachment_inspection_failed")
                    for index in range(page_count):
                        try:
                            page = document.load_page(index)
                            result, attachments = _extract_page(page, index + 1)
                            pages.append(result)
                            embedded.extend(attachments)
                        except Exception:
                            pages.append(ExtractedPage(page_number=index + 1, extraction_status="failed", warnings=["page_loading_failed"]))
        except Exception:
            warnings.append("pdf_open_or_parse_failed")
            if page_count is not None:
                existing = {page.page_number for page in pages}
                pages.extend(ExtractedPage(page_number=index + 1, extraction_status="failed", warnings=["document_parsing_interrupted"])
                             for index in range(page_count) if index + 1 not in existing)
                pages.sort(key=lambda page: page.page_number)
    if page_count == 0:
        warnings.append("pdf_has_no_pages")
    failures = sum(page.extraction_status == "failed" for page in pages)
    status = "failed" if page_count in (None, 0) or failures == page_count else "partial" if failures else "success"
    if failures:
        warnings.append("one_or_more_pages_failed")
    if any(page.requires_ocr for page in pages):
        warnings.append("ocr_may_be_required")
    if any(page.potentially_garbled for page in pages):
        warnings.append("potentially_garbled_text")
    if any(page.extraction_status == "failed" or "embedded_annotation_inspection_failed" in page.warnings or "page_annotation_inspection_failed" in page.warnings for page in pages):
        embedded_status = "partial" if embedded_status == "success" else embedded_status
    try:
        if file_sha256(path) != hashes["sha256"]:
            warnings.append("source_changed_during_extraction")
            status = "failed"
    except Exception:
        warnings.append("post_extraction_integrity_check_failed")
        status = "failed"
    references = [reference for page in pages if page.text is not None for reference in detect_attachment_references(page.text, page.page_number)]
    return ExtractedDocument(document_id=identity.document_id, source_filename=path.name, source_relative_path=relative,
        local_path=str(path), original_filename=identity.original_filename, source_path=identity.source_path,
        original_tags=identity.original_tags, parent_ids=identity.parent_ids, family_ids=identity.family_ids,
        identity=identity, sha256=hashes["sha256"], page_count=page_count, file_size_bytes=size,
        pages=pages, referenced_attachments=references, embedded_attachments=embedded,
        embedded_attachment_inspection_status=embedded_status, extraction_status=status, extraction_warnings=list(dict.fromkeys(warnings)))


def run_extraction(source_dir: Path, *, project_root: Path | None = None, manifest_path: Path | None = None,
                   expected_pdf_count: int = 5) -> tuple[list[ExtractedDocument], dict[str, Any], list[Path]]:
    project_root = project_root or Path.cwd()
    protection = check_secret_protection(project_root)
    destination = project_root / "data/raw/first_pilot"
    paths = [project_root / "data/processed/first_pilot_documents.jsonl", project_root / "data/processed/first_pilot_extraction_report.json"]
    copies, warnings = copy_exports(source_dir, destination)
    pdfs = [item for item in copies if Path(item.relative_path).suffix.lower() == ".pdf"]
    metadata_files = [destination / item.relative_path for item in copies if item.verification_status == "verified" and Path(item.relative_path).suffix.lower() in PRODUCTION_METADATA_SUFFIXES]
    unexpected = [item.relative_path for item in copies if Path(item.relative_path).suffix.lower() not in PRODUCTION_METADATA_SUFFIXES | {".pdf"}]
    if len(pdfs) != expected_pdf_count:
        warnings.append("unexpected_pilot_pdf_count")
    if unexpected:
        warnings.append("unexpected_export_files_preserved")
    if manifest_path is None:
        named = project_root / "data/raw/Walltown_Master_Manifest.xlsx"
        existing = project_root / "data/raw/wallTownManifest.xlsx"
        manifest_path = named if named.is_file() else existing if existing.is_file() else None
        if not named.is_file() and existing.is_file():
            warnings.append("named_master_manifest_missing_used_existing_manifest")
    pilot_path = project_root / "data/pilot/pilot_manifest.csv"
    metadata_sources = [path for path in [manifest_path, pilot_path, *metadata_files] if path is not None and path.is_file()]
    source_hashes_before = {str(path): file_sha256(path) for path in metadata_sources}
    catalog = load_identity_catalog(manifest_path, pilot_path, metadata_files, destination)
    warnings.extend(catalog.warnings)
    records: list[ExtractedDocument] = []
    for item in pdfs:
        path = destination / item.relative_path
        if item.verification_status != "verified":
            records.append(failed_document(path, item.relative_path, "pdf_copy_verification_failed", sha256=item.source_sha256))
            continue
        try:
            records.append(extract_pdf(path, relative_path=item.relative_path, catalog=catalog, expected_sha256=item.source_sha256))
        except Exception:
            records.append(failed_document(path, item.relative_path, "unexpected_document_extraction_failure", sha256=item.source_sha256))
    resolve_attachment_availability(records)
    # Round-trip every record through the public schema before writing it.
    records = [ExtractedDocument.model_validate_json(record.model_dump_json()) for record in records]
    assert len(records) == len(pdfs)
    page_results = [page for record in records for page in record.pages]
    document_warning_counts = Counter(warning for record in records for warning in record.extraction_warnings)
    page_warning_counts = Counter(warning for page in page_results for warning in page.warnings)
    references = [reference for record in records for reference in record.referenced_attachments]
    unchanged = all(path.is_file() and file_sha256(path) == source_hashes_before[str(path)] for path in metadata_sources)
    if not unchanged:
        warnings.append("metadata_source_changed_during_extraction")
    all_copy_verified = all(item.verification_status == "verified" for item in copies)
    report: dict[str, Any] = {
        "schema_version": "1.0", "source_directory": str(source_dir.expanduser()), "copied_directory": str(destination),
        "expected_pdf_count": expected_pdf_count, "total_source_files_discovered": len(copies), "pdf_files_discovered": len(pdfs),
        "missing_expected_pdfs": max(0, expected_pdf_count - len(pdfs)), "unexpected_pdf_count": max(0, len(pdfs) - expected_pdf_count),
        "production_metadata_files_preserved": len(metadata_files), "unexpected_files": unexpected,
        "total_files_successfully_processed": sum(record.extraction_status == "success" for record in records),
        "total_partial_extractions": sum(record.extraction_status == "partial" for record in records),
        "total_extraction_failures": sum(record.extraction_status == "failed" for record in records),
        "total_declared_pages": sum(record.page_count or 0 for record in records),
        "total_page_results": len(page_results),
        "total_pages_processed": sum(page.extraction_status == "success" for page in page_results),
        "total_failed_pages": sum(page.extraction_status == "failed" for page in page_results),
        "documents_with_unknown_page_count": sum(record.page_count is None for record in records),
        "total_extracted_characters": sum(page.character_count or 0 for page in page_results),
        "empty_text_pages": sum(page.text is not None and not page.text.strip() for page in page_results),
        "empty_pages": sum(page.is_blank is True for page in page_results),
        "pages_requiring_ocr": sum(page.requires_ocr is True for page in page_results),
        "pages_with_unknown_ocr_requirement": sum(page.requires_ocr is None for page in page_results),
        "potentially_garbled_pages": sum(page.potentially_garbled for page in page_results),
        "documents_referencing_attachments": sum(bool(record.referenced_attachments) for record in records),
        "attachment_reference_count": len(references),
        "unavailable_attachment_count": sum(reference.availability == "unavailable" for reference in references),
        "unresolved_attachment_name_count": sum(reference.name is None for reference in references),
        "ambiguous_attachment_candidate_count": sum(reference.availability == "ambiguous_candidates" for reference in references),
        "embedded_attachment_count": sum(len(record.embedded_attachments) for record in records),
        "embedded_attachment_inspection_failures": sum(record.embedded_attachment_inspection_status in {"failed", "partial"} for record in records),
        "embedded_attachment_inspection_not_attempted": sum(record.embedded_attachment_inspection_status == "not_attempted" for record in records),
        "documents_with_unresolved_identities": sum(record.identity.status != "resolved" for record in records),
        "identity_status_counts": dict(Counter(record.identity.status for record in records)),
        "identity_mapping_sources": catalog.sources,
        "missing_identity_metadata_counts": {name: sum(getattr(record, name) is None for record in records)
            for name in ("document_id", "original_filename", "source_path", "original_tags", "parent_ids", "family_ids")},
        "document_warning_counts": dict(sorted(document_warning_counts.items())),
        "page_warning_counts": dict(sorted(page_warning_counts.items())), "pipeline_warnings": list(dict.fromkeys(warnings)),
        "copy_verification": {"all_verified": all_copy_verified, "verified_files": sum(item.verification_status == "verified" for item in copies),
                              "failed_files": sum(item.verification_status != "verified" for item in copies), "files": [item.model_dump() for item in copies]},
        "existing_metadata_sources_unchanged": unchanged, "secret_protection": protection,
        "all_source_pdfs_accounted_for": len(records) == len(pdfs),
        "all_declared_pages_accounted_for": len(page_results) == sum(record.page_count or 0 for record in records),
        "output_paths": [str(path) for path in paths],
    }
    report["validation_passed"] = bool(len(pdfs) == expected_pdf_count and all_copy_verified and unchanged and
        report["total_extraction_failures"] == 0 and report["total_partial_extractions"] == 0 and
        report["all_source_pdfs_accounted_for"] and report["all_declared_pages_accounted_for"])

    def write_jsonl(handle: TextIO) -> None:
        for record in records:
            handle.write(record.model_dump_json() + "\n")

    _atomic_write(paths[0], write_jsonl)
    _write_json(paths[1], report)
    # Validate the actual serialized artifact, not just the in-memory records.
    with paths[0].open(encoding="utf-8") as handle:
        serialized = [ExtractedDocument.model_validate_json(line) for line in handle]
    if len(serialized) != len(pdfs):
        raise ExtractionValidationError("Serialized JSONL accounting failed.")
    return records, report, paths
