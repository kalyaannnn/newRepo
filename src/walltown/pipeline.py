"""Write the five local Phase 1 artifacts, never using a network service."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from walltown.analysis import duplicate_rows, summarize_corpus, tag_distribution
from walltown.leakage import build_leakage_groups
from walltown.manifest import ManifestValidationError, file_sha256, read_manifest
from walltown.sampling import select_pilot


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, lambda handle: json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False))


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    def write(handle: TextIO) -> None:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (list, dict)) else value for key, value in row.items()})
    _atomic_write(path, write)


def _atomic_write(path: Path, write: Callable[[TextIO], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            write(handle)
            if path.suffix == ".json":
                handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_phase1(manifest_path: Path, *, data_dir: Path = Path("data"), sheet: str | int | None = None,
               column_map: Mapping[str, str] | None = None, extra_research_tags: tuple[str, ...] = ()) -> tuple[dict[str, Any], dict[str, Any], list[Path]]:
    paths = [
        data_dir / "processed" / "corpus_summary.json", data_dir / "processed" / "tag_distribution.csv",
        data_dir / "processed" / "duplicate_report.csv", data_dir / "pilot" / "pilot_manifest.csv",
        data_dir / "pilot" / "pilot_selection_summary.json",
    ]
    if any(path.resolve() == manifest_path.resolve() for path in paths):
        raise ManifestValidationError("An output path would overwrite the source workbook. Choose a different data directory.")
    manifest = read_manifest(manifest_path, sheet=sheet, column_map=column_map)
    groups = build_leakage_groups(manifest.documents)
    corpus = summarize_corpus(manifest, groups)
    pilot, selection = select_pilot(manifest.documents, groups, extra_research_tags=extra_research_tags)
    selection["source_sha256"] = manifest.source_sha256
    selection["missing_optional_columns"] = manifest.missing_optional_columns
    selection["output_paths"] = [str(path) for path in paths]
    # Verify the read-only source before publishing any output artifact.
    if file_sha256(manifest_path) != manifest.source_sha256:
        raise ManifestValidationError("The source workbook changed during analysis. Outputs were not written.")
    corpus["source"]["unchanged_after_analysis"] = True
    corpus["output_paths"] = [str(path) for path in paths]
    _write_json(paths[0], corpus)
    _write_csv(paths[1], tag_distribution(manifest.documents), ["tag", "normalized_tag", "original_variants", "document_count", "percentage_of_documents"])
    _write_csv(paths[2], duplicate_rows(manifest.documents, groups), ["hash_algorithm", "hash", "duplicate_document_count", "document_id", "leakage_group_id"])
    _write_csv(paths[3], pilot, list(pilot[0]))
    _write_json(paths[4], selection)
    return corpus, selection, paths
