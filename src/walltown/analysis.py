"""Aggregate corpus reports; no document contents are opened."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from typing import Any

from walltown.features import LARGE_FILE_BYTES, LARGE_PAGE_COUNT, difficulty_flags, review_flags
from walltown.leakage import LeakageGroups
from walltown.manifest import ALIASES, Document, Manifest, normalize_tag


def numeric_summary(values: list[int | None], bins: list[tuple[str, int | None]]) -> dict[str, Any]:
    known = sorted(value for value in values if value is not None)
    counts: Counter[str] = Counter({label: 0 for label, _ in bins})
    for value in known:
        for label, maximum in bins:
            if maximum is None or value <= maximum:
                counts[label] += 1
                break

    def quantile(fraction: float) -> float | None:
        if not known:
            return None
        index = (len(known) - 1) * fraction
        lower = int(index)
        upper = min(lower + 1, len(known) - 1)
        return known[lower] + (known[upper] - known[lower]) * (index - lower)

    return {
        "known_document_count": len(known), "missing_invalid_or_conflicting_document_count": len(values) - len(known),
        "total_known": sum(known), "minimum": min(known) if known else None,
        "maximum": max(known) if known else None, "mean": sum(known) / len(known) if known else None,
        "median": quantile(0.5), "p90": quantile(0.9), "p95": quantile(0.95), "p99": quantile(0.99),
        "distribution": dict(counts),
    }


def tag_distribution(documents: dict[str, Document]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    variants: dict[str, set[str]] = defaultdict(set)
    for doc in documents.values():
        counts.update(doc.normalized_tags)
        for tag in doc.tags:
            variants[normalize_tag(tag)].add(tag)
    return [
        {"tag": sorted(variants[tag])[0], "normalized_tag": tag,
         "original_variants": sorted(variants[tag]), "document_count": count,
         "percentage_of_documents": 100 * count / len(documents)}
        for tag, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def duplicate_rows(documents: dict[str, Document], groups: LeakageGroups) -> list[dict[str, Any]]:
    return [
        {"hash_algorithm": algorithm if algorithm != "hash" else "unspecified",
         "hash": value, "duplicate_document_count": len(ids), "document_id": identifier,
         "leakage_group_id": groups.document_groups[identifier]}
        for (algorithm, value), ids in sorted(groups.hash_matches.items()) if len(ids) > 1
        for identifier in sorted(ids)
    ]


def summarize_corpus(manifest: Manifest, groups: LeakageGroups) -> dict[str, Any]:
    docs = manifest.documents
    file_types: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    overlaps: Counter[tuple[str, str]] = Counter()
    family_members: dict[str, set[str]] = defaultdict(set)
    parent_members: dict[str, set[str]] = defaultdict(set)
    difficulties: Counter[str] = Counter()
    sensitivity: Counter[str] = Counter()
    for identifier, doc in docs.items():
        file_types.update(doc.strings("file_type") or ["(missing)"])
        extensions.update(doc.strings("extension") or ["(not supplied)"])
        overlaps.update(combinations(sorted(doc.normalized_tags), 2))
        for family in doc.strings("family_id"):
            family_members[family].add(identifier)
        for parent in doc.strings("parent_id"):
            if parent != identifier:
                parent_members[parent].add(identifier)
        difficulties.update(difficulty_flags(doc))
        sensitivity.update(review_flags(doc))
    duplicate_groups = {key: ids for key, ids in groups.hash_matches.items() if len(ids) > 1}
    duplicate_ids = {identifier for ids in duplicate_groups.values() for identifier in ids}
    metadata_conflicts = {
        field: sum(len(doc.strings(field)) > 1 for doc in docs.values())
        for field in ALIASES if field not in {"tags", "document_id"}
    }

    def largest(field: str) -> list[dict[str, Any]]:
        ordered = sorted((doc for doc in docs.values() if doc.number(field) is not None),
                         key=lambda doc: (-(doc.number(field) or 0), doc.document_id))[:20]
        return [{"document_id": doc.document_id, "filename": doc.first("filename"),
                 "page_count": doc.number("page_count"), "file_size_bytes": doc.number("file_size_bytes"),
                 "file_type": doc.first("file_type"), "preprocessing_flags": difficulty_flags(doc)} for doc in ordered]

    return {
        "source": {"path": str(manifest.source_path), "sha256": manifest.source_sha256,
                   "worksheet": manifest.worksheet, "worksheet_count": manifest.worksheet_count},
        "schema": {"original_columns": manifest.columns, "canonical_mapping": manifest.column_mapping,
                   "required_fields": ["document_id"], "missing_required_columns": [],
                   "missing_optional_columns": manifest.missing_optional_columns,
                   "unmapped_columns_preserved_in_original_metadata": [col for col in manifest.columns if not any(col in sources for sources in manifest.column_mapping.values())]},
        "total_records": manifest.record_count, "unique_document_ids": len(docs),
        "records_without_document_id_excluded_from_sampling": manifest.missing_id_records,
        "repeated_document_id_records": manifest.record_count - manifest.missing_id_records - len(docs),
        "numeric_id_records": manifest.numeric_id_records,
        "numeric_id_note": "Numeric IDs are preserved as read; Excel may already have lost leading zeros or precision before ingestion.",
        "statistics_population": "Unique explicit document IDs. Missing-ID records are excluded; conflicting numeric metadata is not totaled.",
        "file_type_distribution": dict(sorted(file_types.items())),
        "file_type_source": "Explicit MIME/file-type columns only. Filename extensions are not inferred.",
        "explicit_extension_distribution": dict(sorted(extensions.items())),
        "pages": numeric_summary([doc.number("page_count") for doc in docs.values()], [
            ("0", 0), ("1", 1), ("2-5", 5), ("6-20", 20), ("21-100", 100), ("101-500", 500), (">500", None),
        ]),
        "file_sizes_bytes": numeric_summary([doc.number("file_size_bytes") for doc in docs.values()], [
            ("0", 0), ("1-1024", 1024), ("1025-1MiB", 1024**2), ("1MiB-10MiB", 10 * 1024**2),
            ("10MiB-25MiB", 25 * 1024**2), ("25MiB-100MiB", 100 * 1024**2), (">100MiB", None),
        ]),
        "tags": {"unique_normalized_tags": len(tag_distribution(docs)),
                 "untagged_documents": sum("tags" in doc.values and not doc.tags for doc in docs.values()),
                 "documents_without_tag_metadata_column": sum("tags" not in doc.values for doc in docs.values()),
                 "existing_tags_are_ground_truth": False,
                 "overlap_population": "Unique document IDs; case and whitespace normalized only for counting.",
                 "overlap": [{"tag_a": left, "tag_b": right, "document_count": count}
                             for (left, right), count in sorted(overlaps.items(), key=lambda pair: (-pair[1], pair[0]))]},
        "duplicates": {"hash_match_groups": len(duplicate_groups), "documents_in_hash_match_groups": len(duplicate_ids),
                       "documents_with_valid_hash": sum(any(doc.hashes.values()) for doc in docs.values()),
                       "invalid_hash_value_counts": manifest.invalid_hash_values,
                       "matching_filenames_are_not_used_for_content_deduplication": True,
                       "hash_algorithm_note": "Only equal values within the same reported hash algorithm are compared; generic hash algorithm remains unspecified."},
        "families": {"explicit_family_count": len(family_members),
                     "documents_with_family_id": sum(bool(doc.strings("family_id")) for doc in docs.values()),
                     "documents_with_parent_reference": sum(bool(doc.strings("parent_id")) for doc in docs.values()),
                     "nonself_parent_child_links": sum(len(ids) for ids in parent_members.values()),
                     "children_with_observed_parent_document": len({child for parent, children in parent_members.items() if parent in docs for child in children}),
                     "unresolved_parent_ids": len(set(parent_members) - docs.keys()),
                     "self_parent_references": sum(doc.document_id in doc.strings("parent_id") for doc in docs.values()),
                     "family_size_distribution": dict(sorted(Counter(map(len, family_members.values())).items())),
                     "explicit_families": [{"family_id": family, "document_count": len(ids), "document_ids": sorted(ids)}
                                           for family, ids in sorted(family_members.items())],
                     "parent_relationships": [{"parent_id": parent, "child_document_ids": sorted(ids), "parent_present_in_manifest": parent in docs}
                                              for parent, ids in sorted(parent_members.items())],
                     "attachment_note": "Parent relationships are reported as potential family/attachment links; absent attachment metadata is not invented."},
        "missing_metadata": {"source_record_counts": manifest.missing_values,
                             "document_counts": {name: sum(not doc.strings(name) for doc in docs.values()) for name in ALIASES},
                             "invalid_numeric_value_counts": manifest.invalid_numeric_values,
                             "conflicting_values_by_document": metadata_conflicts},
        "preprocessing": {"metadata_only": True, "document_contents_or_text_layers_inspected": False,
                          "large_page_threshold": LARGE_PAGE_COUNT, "large_byte_threshold": LARGE_FILE_BYTES,
                          "challenge_counts": dict(sorted(difficulties.items())),
                          "sensitivity_flag_counts": dict(sorted(sensitivity.items())),
                          "documents_flagged_for_sensitivity_review": sum(bool(review_flags(doc)) for doc in docs.values()),
                          "external_processing_authorized": False,
                          "unflagged_records_are_not_assumed_safe": True},
        "largest_documents_by_pages": largest("page_count"),
        "largest_documents_by_bytes": largest("file_size_bytes"),
        "leakage_protection": {"method": "Transitive components of document IDs, same-algorithm hashes, family IDs, and parent references across the entire manifest.",
                               "group_count": len(groups.members), "largest_group_document_count": max(map(len, groups.members.values()), default=0),
                               "limits": "Undeclared content duplicates or family relationships cannot be identified from absent metadata. Recompute groups and audit split reservations if the manifest changes.",
                               "groups": [{"leakage_group_id": group, "document_ids": ids} for group, ids in groups.members.items()]},
    }
