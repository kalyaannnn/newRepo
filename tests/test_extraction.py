"""Synthetic, local PDFs only. No real case text or credentials are fixtures."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pymupdf
import pytest
from pydantic import ValidationError

from walltown.attachments import detect_attachment_references, resolve_attachment_availability
from walltown.cli import main
from walltown.extraction import ExtractionValidationError, check_secret_protection, copy_exports, extract_pdf, run_extraction
from walltown.extraction_models import ExtractedDocument, ExtractedPage, IdentityResolution
from walltown.identity import file_hashes, load_identity_catalog, resolve_identity


def create_pdf(path: Path, texts: list[str], *, embedded: bool = False, image_only: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        for text in texts:
            page = pdf.new_page()
            if text:
                page.insert_text((40, 60), text)
            if image_only:
                image = b"P6\n10 10\n255\n" + b"\x00\x00\x00" * 100
                page.insert_image(pymupdf.Rect(40, 100, 140, 200), stream=image)
        if embedded:
            pdf.embfile_add("embedded.txt", b"Synthetic attachment bytes", filename="embedded.txt")
        pdf.save(path)
    return path


def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".gitignore").write_text(".env\n.venv/\ndata/\n__pycache__/\n")
    return root


def write_mapping(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_single_page_preserves_exact_extractor_text_and_hash(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "one.pdf", ["Synthetic line one\nSynthetic line two"])
    with pymupdf.open(path) as pdf:
        expected_text = pdf[0].get_text("text", sort=False)
    record = extract_pdf(path)
    assert record.extraction_status == "success"
    assert record.pages[0].text == expected_text
    assert record.pages[0].text.endswith("\n")
    assert record.pages[0].character_count == len(expected_text)
    assert record.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record.file_size_bytes == path.stat().st_size
    assert record.source_filename == "one.pdf"
    assert record.page_count == 1


def test_multiple_pages_have_one_based_numbering_and_preserve_order(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "many.pdf", ["First", "Second", "Third"])
    record = extract_pdf(path)
    assert [page.page_number for page in record.pages] == [1, 2, 3]
    assert [page.text for page in record.pages] == ["First\n", "Second\n", "Third\n"]


def test_blank_pages_are_not_assumed_to_require_ocr(tmp_path: Path) -> None:
    record = extract_pdf(create_pdf(tmp_path / "blank.pdf", [""]))
    page = record.pages[0]
    assert page.text == "" and page.character_count == 0
    assert page.is_blank is True
    assert page.requires_ocr is False
    assert "blank_page" in page.warnings


def test_image_only_page_requires_ocr_without_running_ocr(tmp_path: Path) -> None:
    record = extract_pdf(create_pdf(tmp_path / "image.pdf", [""], image_only=True))
    page = record.pages[0]
    assert page.text == ""
    assert page.image_count == 1
    assert page.is_blank is False
    assert page.requires_ocr is True
    assert "ocr_may_be_required" in record.extraction_warnings


def test_corrupt_pdf_is_an_explicit_failure_with_no_native_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.7\nConfidential synthetic malformed content\n")
    record = extract_pdf(path)
    assert record.extraction_status == "failed"
    assert record.page_count is None
    assert record.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "pdf_open_or_parse_failed" in record.extraction_warnings
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_page_failure_keeps_a_placeholder_and_later_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from walltown import extraction

    path = create_pdf(tmp_path / "partial.pdf", ["First", "Second", "Third"])
    original = extraction._extract_page

    def fail_second(page: object, number: int) -> object:
        if number == 2:
            raise RuntimeError("Synthetic confidential exception detail")
        return original(page, number)

    monkeypatch.setattr(extraction, "_extract_page", fail_second)
    record = extract_pdf(path)
    assert record.extraction_status == "partial"
    assert record.page_count == 3
    assert [page.page_number for page in record.pages] == [1, 2, 3]
    assert record.pages[1].extraction_status == "failed"
    assert record.pages[1].text is None
    assert record.pages[1].character_count is None
    assert record.pages[2].text == "Third\n"
    assert record.embedded_attachment_inspection_status == "partial"


def test_password_protected_pdf_accounts_for_every_page(tmp_path: Path) -> None:
    path = tmp_path / "encrypted.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.new_page()
        pdf.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="synthetic-owner", user_pw="synthetic-user")
    record = extract_pdf(path)
    assert record.extraction_status == "failed"
    assert record.page_count == len(record.pages) == 2
    assert all(page.extraction_status == "failed" for page in record.pages)
    assert "pdf_requires_password" in record.extraction_warnings


@pytest.mark.parametrize("header", ["Attachments: report.pdf; sheet.xlsx\n", "Attachment: report.pdf\n", "Attachments:\nreport.pdf\nsheet.xlsx\n", "Attachments\nreport.pdf\n"])
def test_attachment_headers_and_offsets(header: str) -> None:
    text = "From: synthetic@example.invalid\n" + header + "Subject: Synthetic\n\nBody remains unchanged.\n"
    references = detect_attachment_references(text, 2)
    assert references
    assert references[0].name == "report.pdf"
    assert references[0].page_number == 2
    for reference in references:
        assert text[reference.source_start:reference.source_end] == reference.raw_header_text
    assert "Body remains unchanged." not in references[0].raw_header_text


def test_body_mentions_without_attachment_header_are_not_references() -> None:
    assert not detect_attachment_references("Please read the attachment report.pdf in the next email.\n", 1)


def test_missing_attachment_is_a_warning_not_an_extraction_failure(tmp_path: Path) -> None:
    record = extract_pdf(create_pdf(tmp_path / "email.pdf", ["Attachments: missing.pdf\nSubject: Synthetic\n\nBody"] ))
    original_text = record.pages[0].text
    resolve_attachment_availability([record])
    assert record.referenced_attachments[0].availability == "unavailable"
    assert "referenced_attachment_unavailable" in record.extraction_warnings
    assert record.extraction_status == "success"
    assert record.pages[0].text == original_text


def test_filename_candidate_does_not_establish_parent_relationship(tmp_path: Path) -> None:
    parent = extract_pdf(create_pdf(tmp_path / "email.pdf", ["Attachments: attachment.pdf\n\nBody"]))
    child = extract_pdf(create_pdf(tmp_path / "attachment.pdf", ["Synthetic attachment text"]))
    resolve_attachment_availability([parent, child])
    reference = parent.referenced_attachments[0]
    assert reference.availability == "exported_candidate"
    assert reference.exported_file_candidates == ["attachment.pdf"]
    assert not reference.parent_child_relationship_verified
    assert parent.parent_ids is None and child.parent_ids is None


def test_embedded_attachment_is_recorded_separately_without_extracting_payload(tmp_path: Path) -> None:
    record = extract_pdf(create_pdf(tmp_path / "embedded.pdf", ["Attachments: embedded.txt\n\nBody"], embedded=True))
    resolve_attachment_availability([record])
    assert len(record.embedded_attachments) == 1
    assert record.embedded_attachments[0].filename == "embedded.txt"
    assert record.embedded_attachments[0].storage == "embedded_files"
    assert record.referenced_attachments[0].availability == "embedded_candidate"
    assert not record.referenced_attachments[0].exported_file_candidates
    assert not (tmp_path / "embedded.txt").exists()


def test_page_file_attachment_annotation_is_also_inspected(tmp_path: Path) -> None:
    path = tmp_path / "annotation.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.add_file_annot((60, 60), b"Synthetic attachment", "note.txt")
        pdf.save(path)
    record = extract_pdf(path)
    assert len(record.embedded_attachments) == 1
    assert record.embedded_attachments[0].storage == "file_attachment_annotation"
    assert record.embedded_attachments[0].page_number == 1


def test_identity_is_unresolved_without_verified_mapping_even_with_same_name(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "1.pdf", ["Synthetic text"])
    manifest = tmp_path / "manifest.xlsx"
    pd.DataFrame([{"GF ID": "1", "File Name": "1.pdf", "MD5 Hash": "f" * 32}]).to_excel(manifest, index=False)
    catalog = load_identity_catalog(manifest, None, [], tmp_path)
    record = extract_pdf(path, catalog=catalog)
    assert record.document_id is None
    assert record.original_filename is None and record.source_path is None
    assert record.original_tags is None and record.family_ids is None
    assert "identity_unresolved" in record.extraction_warnings


def test_unique_native_hash_resolves_and_preserves_goldfynch_metadata(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "export.pdf", ["Synthetic text"])
    hashes = file_hashes(path)
    manifest = tmp_path / "manifest.xlsx"
    pd.DataFrame([{"GF ID": "g-one", "File Name": "original.pdf", "GF Folder": "/synthetic/folder",
                   "MD5 Hash": hashes["md5"], "Tags": "A;B", "Parent ID": "parent", "Family ID": "family"}]).to_excel(manifest, index=False)
    catalog = load_identity_catalog(manifest, None, [], tmp_path)
    record = extract_pdf(path, catalog=catalog)
    assert record.document_id == "g-one"
    assert record.original_filename == "original.pdf"
    assert record.source_path == "/synthetic/folder"
    assert record.original_tags == ["A", "B"]
    assert record.parent_ids == ["parent"] and record.family_ids == ["family"]
    assert record.identity.evidence[0]["method"] == "exact_content_hash"


def test_hash_duplicate_ids_are_ambiguous_not_silently_chosen(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "same.pdf", ["Synthetic text"])
    manifest = tmp_path / "manifest.xlsx"
    md5 = file_hashes(path)["md5"]
    pd.DataFrame([{"GF ID": identifier, "MD5 Hash": md5} for identifier in ["a", "b"]]).to_excel(manifest, index=False)
    record = extract_pdf(path, catalog=load_identity_catalog(manifest, None, [], tmp_path))
    assert record.identity.status == "ambiguous"
    assert record.document_id is None
    assert record.identity.candidate_document_ids == ["a", "b"]


def test_explicit_export_mapping_can_resolve_converted_pdf_without_matching_native_hash(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "1.pdf", ["Synthetic converted email"])
    mapping = write_mapping(tmp_path / "mapping.csv", [{"Export Filename": "1.pdf", "GF ID": "g-email", "File Name": "original.eml", "MD5 Hash": "f" * 32}])
    catalog = load_identity_catalog(None, None, [mapping], tmp_path)
    record = extract_pdf(path, catalog=catalog)
    assert record.document_id == "g-email"
    assert record.original_filename == "original.eml"
    assert record.identity.status == "resolved"


def test_declared_export_hash_conflict_keeps_identity_null(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "1.pdf", ["Synthetic"])
    mapping = write_mapping(tmp_path / "mapping.csv", [{"Export Filename": "1.pdf", "GF ID": "g-one", "Export SHA256": "f" * 64}])
    record = extract_pdf(path, catalog=load_identity_catalog(None, None, [mapping], tmp_path))
    assert record.document_id is None
    assert record.identity.status == "conflict"


def test_conflicting_production_mappings_remain_ambiguous(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "1.pdf", ["Synthetic"])
    mapping = write_mapping(tmp_path / "mapping.csv", [{"Export Filename": "1.pdf", "GF ID": identifier} for identifier in ["g-one", "g-two"]])
    record = extract_pdf(path, catalog=load_identity_catalog(None, None, [mapping], tmp_path))
    assert record.identity.status == "ambiguous" and record.document_id is None


def test_generic_production_id_cannot_be_assumed_to_be_goldfynch_id(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "1.pdf", ["Synthetic"])
    mapping = write_mapping(tmp_path / "mapping.csv", [{"Export Filename": "1.pdf", "Document ID": "unknown-bates-number"}])
    catalog = load_identity_catalog(None, None, [mapping], tmp_path)
    assert "production_identifier_not_verified_as_goldfynch_id" in catalog.warnings
    assert resolve_identity("1.pdf", file_hashes(path), catalog).document_id is None


def test_pilot_manifest_can_supply_verified_native_hash_identity(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "1.pdf", ["Synthetic"])
    metadata = [{"GF ID": "pilot-one", "File Name": "original.pdf", "MD5 Hash": file_hashes(path)["md5"]}]
    pilot = write_mapping(tmp_path / "pilot.csv", [{"document_id": "pilot-one", "original_metadata": json.dumps(metadata)}])
    catalog = load_identity_catalog(None, pilot, [], tmp_path)
    assert extract_pdf(path, catalog=catalog).document_id == "pilot-one"


def test_copy_preserves_nested_names_metadata_and_source_hashes(tmp_path: Path) -> None:
    source = tmp_path / "downloads"
    create_pdf(source / "nested/1.pdf", ["Synthetic"])
    (source / "nested/production.dat").write_text("Synthetic opaque load file")
    destination = tmp_path / "project/data/raw/first_pilot"
    before = {path.relative_to(source): file_hashes(path)["sha256"] for path in source.rglob("*") if path.is_file()}
    copies, _ = copy_exports(source, destination)
    assert len(copies) == 2 and all(item.verification_status == "verified" for item in copies)
    for relative, expected in before.items():
        assert file_hashes(source / relative)["sha256"] == file_hashes(destination / relative)["sha256"] == expected
    again, _ = copy_exports(source, destination)
    assert all(item.action == "verified_existing_copy" for item in again)


def test_copy_does_not_overwrite_a_conflicting_destination(tmp_path: Path) -> None:
    source = tmp_path / "downloads"
    create_pdf(source / "1.pdf", ["Source"])
    destination = tmp_path / "copied"
    create_pdf(destination / "1.pdf", ["Existing different content"])
    before = (destination / "1.pdf").read_bytes()
    copies, _ = copy_exports(source, destination)
    assert copies[0].verification_status == "failed"
    assert copies[0].action == "destination_conflict_not_overwritten"
    assert (destination / "1.pdf").read_bytes() == before


def test_copy_refuses_symlink_destinations(tmp_path: Path) -> None:
    source = tmp_path / "source"
    create_pdf(source / "one.pdf", ["Synthetic"])
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExtractionValidationError, match="symlink"):
        copy_exports(source, alias)


def test_jsonl_round_trip_and_summary_have_no_full_text(tmp_path: Path) -> None:
    root = project(tmp_path)
    source = tmp_path / "downloads"
    create_pdf(source / "1.pdf", ["Distinctive synthetic text never printed in summary"])
    create_pdf(source / "2.pdf", ["Attachments: absent.pdf\n\nSynthetic body"])
    records, report, paths = run_extraction(source, project_root=root, expected_pdf_count=2)
    lines = paths[0].read_text().splitlines()
    parsed = [ExtractedDocument.model_validate_json(line) for line in lines]
    assert len(parsed) == len(records) == 2
    assert report["validation_passed"] is True
    assert report["all_source_pdfs_accounted_for"] and report["all_declared_pages_accounted_for"]
    assert report["total_pages_processed"] == 2
    assert report["total_extracted_characters"] == sum(len(page.text or "") for record in records for page in record.pages)
    assert report["unavailable_attachment_count"] == 1
    assert report["documents_with_unresolved_identities"] == 2
    assert "Distinctive synthetic text" not in paths[1].read_text()
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)


def test_failed_files_are_never_silently_skipped_and_count_mismatch_is_reported(tmp_path: Path) -> None:
    root = project(tmp_path)
    source = tmp_path / "downloads"
    create_pdf(source / "good.pdf", ["Synthetic"])
    (source / "bad.pdf").write_bytes(b"not a PDF")
    records, report, paths = run_extraction(source, project_root=root, expected_pdf_count=5)
    assert len(records) == 2
    assert report["pdf_files_discovered"] == 2
    assert report["total_files_successfully_processed"] == 1
    assert report["total_extraction_failures"] == 1
    assert report["missing_expected_pdfs"] == 3
    assert report["all_source_pdfs_accounted_for"]
    assert not report["validation_passed"]
    assert len(paths[0].read_text().splitlines()) == 2


def test_copy_failure_still_gets_one_failed_document_record(tmp_path: Path) -> None:
    root = project(tmp_path)
    source = tmp_path / "downloads"
    create_pdf(source / "one.pdf", ["Synthetic source"])
    create_pdf(root / "data/raw/first_pilot/one.pdf", ["Different destination"])
    records, report, _ = run_extraction(source, project_root=root, expected_pdf_count=1)
    assert len(records) == 1
    assert records[0].extraction_status == "failed"
    assert "pdf_copy_verification_failed" in records[0].extraction_warnings
    assert report["copy_verification"]["failed_files"] == 1


def test_pydantic_rejects_changed_character_counts_and_missing_pages() -> None:
    with pytest.raises(ValidationError, match="Character count"):
        ExtractedPage(page_number=1, text="Synthetic", character_count=1)
    with pytest.raises(ValidationError, match="Every declared page"):
        ExtractedDocument(source_filename="one.pdf", source_relative_path="one.pdf", local_path="one.pdf", page_count=1)


def test_cli_does_not_print_contents_names_or_case_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = project(tmp_path)
    source = tmp_path / "downloads"
    create_pdf(source / "CONFIDENTIAL-name.pdf", ["CONFIDENTIAL synthetic@example.invalid body"])
    monkeypatch.chdir(root)
    assert main(["extract", "--source-dir", str(source), "--expected-pdfs", "1"]) == 0
    captured = capsys.readouterr()
    assert "PDFs successfully extracted: 1" in captured.out
    for sensitive in ["CONFIDENTIAL", "synthetic@", "CONFIDENTIAL-name.pdf"]:
        assert sensitive not in captured.out and sensitive not in captured.err


def test_missing_source_fails_clearly_without_outputs(tmp_path: Path) -> None:
    root = project(tmp_path)
    with pytest.raises(ExtractionValidationError, match="directory is missing"):
        run_extraction(tmp_path / "absent", project_root=root)
    assert not (root / "data").exists()


def test_tracked_protected_files_are_reported_without_history_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project(tmp_path)
    repo = MagicMock(returncode=0, stdout=b"synthetic-root\n")
    tracked = MagicMock(returncode=0, stdout=b"data/synthetic.pdf\0")
    calls = MagicMock(side_effect=[repo, tracked])
    monkeypatch.setattr("walltown.extraction.subprocess.run", calls)
    with pytest.raises(ExtractionValidationError, match="Git-tracked"):
        check_secret_protection(root)
    assert calls.call_count == 2
    assert all("reset" not in str(call) and "filter" not in str(call) for call in calls.call_args_list)
