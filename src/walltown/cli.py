"""Command-line output is restricted to aggregates and generated file paths."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from walltown.extraction import ExtractionValidationError, run_extraction
from walltown.manifest import ManifestValidationError
from walltown.pipeline import run_phase1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="walltown", description="Local GoldFynch manifest analysis, pilot sampling, and PDF extraction.")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="Run Phase 1 locally without opening document contents or making API calls.")
    analyze.add_argument("--manifest", type=Path, default=Path("data/raw/wallTownManifest.xlsx"))
    analyze.add_argument("--data-dir", type=Path, default=Path("data"), help="Root for processed/ and pilot/ output directories.")
    analyze.add_argument("--sheet", help="Exact worksheet name; otherwise detect the one sheet with a document ID column.")
    analyze.add_argument("--column-map", type=Path, help="Local JSON object mapping canonical fields to source headers.")
    analyze.add_argument("--research-tag", action="append", default=[], help="Additional research tag to include in the sampling stratum.")
    extract = commands.add_parser("extract", help="Copy and extract the local five-PDF pilot; no OCR or external API calls.")
    extract.add_argument("--source-dir", type=Path, default=Path.home() / "Downloads/firstPilot")
    extract.add_argument("--manifest", type=Path, help="Local master manifest; otherwise prefer the named master or existing Phase 1 manifest.")
    extract.add_argument("--expected-pdfs", type=int, default=5, help="Expected local PDF count, checked in the extraction report.")
    args = parser.parse_args(argv)
    if args.command == "extract":
        return _extract_command(args)
    try:
        mapping = None
        if args.column_map:
            try:
                mapping = json.loads(args.column_map.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ManifestValidationError("Cannot read --column-map as a local JSON mapping.") from None
            if not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in mapping.items()):
                raise ManifestValidationError("--column-map must contain a JSON object of canonical fields and source-header strings.")
        corpus, selection, paths = run_phase1(args.manifest, data_dir=args.data_dir, sheet=args.sheet,
                                            column_map=mapping, extra_research_tags=tuple(args.research_tag))
    except ManifestValidationError as error:
        print(f"Validation error: {error}", file=sys.stderr)
        return 2
    except OSError:
        print("Local file access failed. Check input and output permissions and available disk space.", file=sys.stderr)
        return 2
    except Exception:
        # Raw exceptions can contain cell values, filenames, URLs, or case text.
        print("Analysis failed. No confidential exception details were printed; validate the local schema and configuration.", file=sys.stderr)
        return 1
    print(f"Records: {corpus['total_records']}")
    print(f"Unique document IDs: {corpus['unique_document_ids']}")
    print(f"Known pages: {corpus['pages']['total_known']}")
    print(f"Documents without usable page counts: {corpus['pages']['missing_invalid_or_conflicting_document_count']}")
    print(f"Known file bytes: {corpus['file_sizes_bytes']['total_known']}")
    print(f"Missing optional columns: {len(corpus['schema']['missing_optional_columns'])}")
    print(f"Records without document IDs: {corpus['records_without_document_id_excluded_from_sampling']}")
    print(f"Hash duplicate groups: {corpus['duplicates']['hash_match_groups']}")
    print(f"Leakage groups: {corpus['leakage_protection']['group_count']}")
    print(f"Pilot unique documents: {selection['selected_unique_documents']}")
    print(f"Identifiable random sample: {selection['random_corpus_sample_count']}")
    print(f"Evaluation-reserved pilot documents: {selection['split_counts'].get('evaluation', 0)}")
    print(f"Development pilot documents: {selection['split_counts'].get('development', 0)}")
    print(f"Sensitivity review flags in pilot: {selection['flagged_for_sensitivity_review']}")
    print(f"Stratum shortage slots: {sum(stats['shortage'] for stats in selection['groups'].values())}")
    print(f"Shortage slots filled: {sum(stats['shortage_filled'] for stats in selection['groups'].values())}")
    for path in paths:
        print(path)
    return 0


def _extract_command(args: argparse.Namespace) -> int:
    if args.expected_pdfs < 1:
        print("Validation error: expected PDF count must be positive.", file=sys.stderr)
        return 2
    try:
        _, report, paths = run_extraction(args.source_dir, manifest_path=args.manifest, expected_pdf_count=args.expected_pdfs)
    except ExtractionValidationError as error:
        print(f"Validation error: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("Local extraction failed. Confidential exception details were suppressed; check local input and output access.", file=sys.stderr)
        return 1
    for label, key in [
        ("Source files discovered", "total_source_files_discovered"), ("PDFs discovered", "pdf_files_discovered"),
        ("PDFs successfully extracted", "total_files_successfully_processed"), ("Partial extractions", "total_partial_extractions"),
        ("Extraction failures", "total_extraction_failures"), ("Pages processed", "total_pages_processed"),
        ("Page failures", "total_failed_pages"), ("Extracted characters", "total_extracted_characters"),
        ("Blank pages", "empty_pages"), ("Pages requiring OCR", "pages_requiring_ocr"),
        ("Attachment references", "attachment_reference_count"), ("Unavailable referenced attachments", "unavailable_attachment_count"),
        ("Unresolved attachment names", "unresolved_attachment_name_count"), ("Embedded attachments", "embedded_attachment_count"),
        ("Unresolved GoldFynch identities", "documents_with_unresolved_identities"),
    ]:
        print(f"{label}: {report[key]}")
    print(f"Verified file copies: {report['copy_verification']['verified_files']}")
    print(f"Failed file copies: {report['copy_verification']['failed_files']}")
    print(f"Extraction validation passed: {report['validation_passed']}")
    for path in paths:
        print(path)
    return 0 if report["validation_passed"] else 1
