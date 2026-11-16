from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from walltown.cli import main
from walltown.pipeline import run_phase1


def synthetic_workbook(path: Path) -> None:
    pd.DataFrame([
        {"GF ID": "one", "File Name": "CONFIDENTIAL jane@example.invalid.pdf", "GF Link": "https://synthetic.invalid/private-case", "Tags": "A;B", "Page Count": 0, "Byte Count": 10, "MD5 Hash": "a" * 32, "Family ID": "family"},
        {"GF ID": "two", "File Name": "CONFIDENTIAL jane@example.invalid.pdf", "Tags": "A", "Page Count": 4, "Byte Count": 20, "MD5 Hash": "b" * 32},
        {"GF ID": "three", "File Name": "different.pdf", "Tags": "B", "Page Count": "", "Byte Count": 30, "MD5 Hash": "a" * 32, "Parent ID": "one"},
        {"GF ID": "one", "File Name": "CONFIDENTIAL jane@example.invalid.pdf", "Tags": "C", "Page Count": 0, "Byte Count": 10, "MD5 Hash": "a" * 32},
    ]).to_excel(path, index=False, engine="openpyxl")


def test_end_to_end_outputs_counts_preservation_and_source_integrity(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.xlsx"
    synthetic_workbook(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    corpus, selection, paths = run_phase1(source, data_dir=tmp_path / "data")
    assert len(paths) == 5 and all(path.is_file() for path in paths)
    assert before == hashlib.sha256(source.read_bytes()).hexdigest()
    assert corpus["source"]["unchanged_after_analysis"]
    assert corpus["total_records"] == 4
    assert corpus["unique_document_ids"] == 3
    assert corpus["repeated_document_id_records"] == 1
    assert corpus["pages"]["total_known"] == 4
    assert corpus["pages"]["distribution"]["0"] == 1
    assert corpus["pages"]["missing_invalid_or_conflicting_document_count"] == 1
    assert corpus["file_sizes_bytes"]["total_known"] == 60
    assert corpus["duplicates"]["hash_match_groups"] == 1
    assert corpus["duplicates"]["documents_in_hash_match_groups"] == 2
    assert corpus["tags"]["overlap"] == [
        {"tag_a": "a", "tag_b": "b", "document_count": 1},
        {"tag_a": "a", "tag_b": "c", "document_count": 1},
        {"tag_a": "b", "tag_b": "c", "document_count": 1},
    ]
    assert selection["selected_unique_documents"] == 3
    with paths[1].open(newline="") as handle:
        tags = {row["normalized_tag"]: int(row["document_count"]) for row in csv.DictReader(handle)}
    assert tags == {"a": 2, "b": 2, "c": 1}
    with paths[2].open(newline="") as handle:
        duplicates = list(csv.DictReader(handle))
    assert {row["document_id"] for row in duplicates} == {"one", "three"}
    with paths[3].open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    one = next(row for row in rows if row["document_id"] == "one")
    assert len(json.loads(one["original_metadata"])) == 2
    assert json.loads(one["original_metadata"])[0]["GF Link"] == "https://synthetic.invalid/private-case"
    assert one["page_count"] == "0"
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    with paths[0].open() as handle:
        assert json.load(handle)["total_records"] == 4


def test_cli_prints_only_aggregates_and_output_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "synthetic.xlsx"
    synthetic_workbook(source)
    assert main(["analyze", "--manifest", str(source), "--data-dir", str(tmp_path / "out")]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "Records: 4" in output.out
    for sensitive in ["CONFIDENTIAL", "jane@", "private-case", "different.pdf", "a" * 32]:
        assert sensitive not in output.out
    assert "pilot_manifest.csv" in output.out


def test_cli_missing_manifest_creates_no_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output_root = tmp_path / "data"
    assert main(["analyze", "--manifest", str(tmp_path / "absent.xlsx"), "--data-dir", str(output_root)]) == 2
    assert "Manifest is required" in capsys.readouterr().err
    assert not output_root.exists()


def test_raw_exceptions_never_expose_values(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("CONFIDENTIAL jane@example.invalid case-link")

    monkeypatch.setattr("walltown.cli.run_phase1", explode)
    assert main(["analyze"]) == 1
    output = capsys.readouterr()
    assert "CONFIDENTIAL" not in output.err
    assert "jane@" not in output.err


def test_duplicate_csv_has_headers_when_no_duplicate_groups_exist(tmp_path: Path) -> None:
    source = tmp_path / "one.xlsx"
    pd.DataFrame([{"GF ID": "one"}]).to_excel(source, index=False, engine="openpyxl")
    corpus, _, paths = run_phase1(source, data_dir=tmp_path / "out")
    assert corpus["tags"]["untagged_documents"] == 0
    assert corpus["tags"]["documents_without_tag_metadata_column"] == 1
    with paths[2].open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames and "document_id" in reader.fieldnames
        assert list(reader) == []
