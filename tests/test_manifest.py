from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from walltown.manifest import ManifestValidationError, parse_count, parse_tags, read_manifest


def workbook(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "synthetic.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False, engine="openpyxl")
    return path


def test_goldfynch_schema_and_original_metadata_are_preserved(tmp_path: Path) -> None:
    row = {"GF ID": "000123", "GF Link": "https://synthetic.invalid/case/item", "File Name": "synthetic.pdf",
           "GF Folder": "/synthetic", "Tags": "IRRELEVANT; SOIL TESTING RESULTS", "Page Count": 0,
           "Byte Count": 0, "MIME Type": "application/pdf", "MD5 Hash": "A" * 32,
           "Parent ID": "p", "Family ID": "f", "Primary Date": "2025-01-01", "Redacted?": "Yes", "Custom": "retained"}
    path = workbook(tmp_path, [row])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = read_manifest(path)
    doc = manifest.documents["000123"]
    assert manifest.column_mapping["document_id"] == ["GF ID"]
    assert doc.first("folder_path") == "/synthetic"
    assert doc.number("page_count") == 0
    assert doc.number("file_size_bytes") == 0
    assert doc.tags == ["IRRELEVANT", "SOIL TESTING RESULTS"]
    assert doc.hashes["md5"] == ["a" * 32]
    assert doc.original_metadata == [row]
    assert doc.source_rows == [2]
    assert before == manifest.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_metadata_stays_missing_and_required_id_is_validated(tmp_path: Path) -> None:
    path = workbook(tmp_path, [{"Document ID": "NA", "File Name": "no-guesses.pdf"}, {"Document ID": "", "File Name": "missing-id.pdf"}])
    manifest = read_manifest(path)
    assert "NA" in manifest.documents  # A literal ID is not a pandas missing-value sentinel.
    assert manifest.missing_id_records == 1
    assert "page_count" in manifest.missing_optional_columns
    assert manifest.documents["NA"].number("page_count") is None
    assert manifest.documents["NA"].first("file_type") is None
    assert manifest.documents["NA"].hashes == {}
    path = workbook(tmp_path, [{"File Name": "synthetic.pdf"}])
    with pytest.raises(ManifestValidationError, match="Missing required column: document_id"):
        read_manifest(path)


def test_missing_workbook_and_all_empty_ids_raise_clear_errors(tmp_path: Path) -> None:
    with pytest.raises(ManifestValidationError, match="Manifest is required"):
        read_manifest(tmp_path / "absent.xlsx")
    path = workbook(tmp_path, [{"GF ID": "", "File Name": "synthetic.pdf"}])
    with pytest.raises(ManifestValidationError, match="No nonempty document IDs"):
        read_manifest(path)


def test_repeated_ids_keep_all_metadata_and_do_not_guess_conflicting_counts(tmp_path: Path) -> None:
    path = workbook(tmp_path, [
        {"GF ID": "same", "File Name": "one.pdf", "Tags": "A", "Page Count": 3},
        {"GF ID": "same", "File Name": "two.pdf", "Tags": "B", "Page Count": 4},
    ])
    manifest = read_manifest(path)
    assert manifest.record_count == 2
    assert len(manifest.documents) == 1
    doc = manifest.documents["same"]
    assert len(doc.original_metadata) == 2
    assert doc.strings("filename") == ["one.pdf", "two.pdf"]
    assert doc.number("page_count") is None
    assert doc.tags == ["A", "B"]


def test_sheet_detection_ignores_summary_and_requires_disambiguation(tmp_path: Path) -> None:
    path = tmp_path / "sheets.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame([{"MIME Type": "application/pdf", "File Count": 1}]).to_excel(writer, sheet_name="Totals", index=False)
        pd.DataFrame([{"GF ID": "one"}]).to_excel(writer, sheet_name="Docs", index=False)
    assert read_manifest(path).worksheet == "Docs"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name in ["A", "B"]:
            pd.DataFrame([{"GF ID": "one"}]).to_excel(writer, sheet_name=name, index=False)
    with pytest.raises(ManifestValidationError, match="Multiple document worksheets"):
        read_manifest(path)
    assert read_manifest(path, sheet="B").worksheet == "B"
    with pytest.raises(ManifestValidationError, match="requested worksheet"):
        read_manifest(path, sheet="absent")


def test_explicit_column_mapping_and_conflicting_ids(tmp_path: Path) -> None:
    path = workbook(tmp_path, [{"Unusual ID Header": "x", "Unusual Bytes Header": 42}])
    manifest = read_manifest(path, column_map={"document_id": "Unusual ID Header", "file_size_bytes": "Unusual Bytes Header"})
    assert manifest.documents["x"].number("file_size_bytes") == 42
    path = workbook(tmp_path, [{"GF ID": "one", "Document ID": "two"}])
    with pytest.raises(ManifestValidationError, match="Conflicting document IDs"):
        read_manifest(path)


def test_mapping_overrides_skip_non_document_sheets(tmp_path: Path) -> None:
    path = tmp_path / "mapped-sheets.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame([{"File Count": 1}]).to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame([{"Custom Identifier": "one"}]).to_excel(writer, sheet_name="Documents", index=False)
    manifest = read_manifest(path, column_map={"document_id": "Custom Identifier"})
    assert manifest.worksheet == "Documents"
    assert set(manifest.documents) == {"one"}


@pytest.mark.parametrize(("value", "expected"), [("", None), (None, None), ("-1", None), ("1.5", None), ("12 MB", None), (True, None), ("1,234", 1234), (0, 0), (3.0, 3)])
def test_numeric_metadata_never_fills_blanks_or_unknown_units(value: object, expected: int | None) -> None:
    assert parse_count(value) == expected


@pytest.mark.parametrize(("value", "expected"), [
    ("A; B", ["A", "B"]), ("A\nB", ["A", "B"]), ("A|B", ["A", "B"]),
    ('"A,B",C', ["A,B", "C"]), ('["A,B", "C"]', ["A,B", "C"]), ("", []), ("NA", ["NA"]),
])
def test_tag_serializations(value: str, expected: list[str]) -> None:
    assert parse_tags(value) == expected
