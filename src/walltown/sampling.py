"""Reproducible, disjoint pilot selection with explicit evaluation reservations."""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from typing import Any

from walltown.features import RESEARCH_PATTERN, RESEARCH_TAGS, difficulty_flags, discover_research_tags, review_flags
from walltown.leakage import LeakageGroups
from walltown.manifest import Document, normalize_tag


SEED = 42
TARGETS = {
    "random_corpus": 100,
    "irrelevant_tag": 75,
    "research_tags": 50,
    "rec_center_development": 40,
    "untagged_or_difficult": 35,
}


@dataclass
class Selection:
    document_id: str
    sampling_group: str
    selection_reason: str
    fills_shortage_for: str | None = None


def select_pilot(documents: dict[str, Document], groups: LeakageGroups, *, extra_research_tags: tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rng = random.Random(SEED)
    corpus = sorted(documents)
    selected: dict[str, Selection] = {}
    group_summary: dict[str, dict[str, int]] = {}
    research_tags = discover_research_tags(documents, extra_research_tags)
    research_set = set(research_tags)

    def matches(identifier: str, group: str) -> bool:
        doc = documents[identifier]
        if group == "random_corpus":
            return True
        if group == "irrelevant_tag":
            return "irrelevant" in doc.normalized_tags
        if group == "research_tags":
            return bool(doc.normalized_tags & research_set)
        if group == "rec_center_development":
            return "rec center development" in doc.normalized_tags
        return ("tags" in doc.values and not doc.tags) or bool(difficulty_flags(doc))

    def reason(identifier: str, group: str) -> str:
        doc = documents[identifier]
        if group == "random_corpus":
            return "Uniform random sample without replacement from all unique explicit document IDs; seed 42. Existing tags are not labels."
        if group in {"irrelevant_tag", "rec_center_development"}:
            return "Existing GoldFynch tag matched the requested sampling stratum; tag is not a ground-truth label."
        if group == "research_tags":
            return "Existing tag matched an explicit research tag or the documented research keyword rule; tag is not a ground-truth label."
        flags = difficulty_flags(doc)
        return "Metadata-only sampling indicators: " + "; ".join((["no_tags_reported"] if "tags" in doc.values and not doc.tags else []) + flags) + "."

    for group, target in TARGETS.items():
        candidates = [identifier for identifier in corpus if identifier not in selected and matches(identifier, group)]
        chosen = rng.sample(candidates, min(target, len(candidates)))
        for identifier in chosen:
            selected[identifier] = Selection(identifier, group, reason(identifier, group))
        group_summary[group] = {"target": target, "eligible_in_corpus": sum(matches(identifier, group) for identifier in corpus),
                                "eligible_after_previous_groups": len(candidates), "selected_primary": len(chosen),
                                "shortage": target - len(chosen), "shortage_filled": 0, "unfilled_shortage": target - len(chosen)}

    # Fill after every primary stratum, so filling an early shortage cannot steal
    # the only eligible records from a later requested stratum.
    remaining = [identifier for identifier in corpus if identifier not in selected]
    rng.shuffle(remaining)
    for group, stats in group_summary.items():
        for _ in range(min(stats["shortage"], len(remaining))):
            identifier = remaining.pop()
            selected[identifier] = Selection(identifier, "shortage_fill",
                "Uniform draw from the remaining corpus to fill an exhausted stratum; does not satisfy that stratum's tag criteria.", group)
            stats["shortage_filled"] += 1
        stats["unfilled_shortage"] = stats["shortage"] - stats["shortage_filled"]

    random_ids = {identifier for identifier, entry in selected.items() if entry.sampling_group == "random_corpus"}
    reserved = {groups.document_groups[identifier] for identifier in random_ids}
    rows: list[dict[str, Any]] = []
    for identifier, entry in selected.items():
        doc = documents[identifier]
        group_id = groups.document_groups[identifier]
        evaluation = group_id in reserved
        flags = review_flags(doc)
        hashes = {algorithm: values for algorithm, values in doc.hashes.items() if values}
        first_hash = next(((algorithm, value) for algorithm in ("sha256", "sha512", "sha1", "md5", "hash") for value in hashes.get(algorithm, [])), None)
        rows.append({
            "document_id": identifier, "filename": doc.first("filename"), "folder_path": doc.first("folder_path"),
            "original_tags": doc.tags, "original_tag_values": doc.values.get("tags", []),
            "page_count": doc.number("page_count"), "file_size_bytes": doc.number("file_size_bytes"),
            "file_type": doc.first("file_type"), "hash_algorithm": first_hash[0] if first_hash else None,
            "hash": first_hash[1] if first_hash else None, "all_hashes": hashes,
            "parent_ids": doc.strings("parent_id"), "family_ids": doc.strings("family_id"),
            "dates": {name: values for name, values in doc.values.items() if name.endswith("_date")},
            "sampling_group": entry.sampling_group, "selection_reason": entry.selection_reason,
            "fills_shortage_for": entry.fills_shortage_for, "leakage_group_id": group_id,
            "split": "evaluation" if evaluation else "development",
            "is_random_corpus_sample": identifier in random_ids,
            "include_in_unbiased_evaluation": identifier in random_ids,
            "split_reason": "Same full-corpus component as a random evaluation document; reserve the entire component." if evaluation else "No known hash/family/parent connection to the reserved random evaluation sample.",
            "sensitivity_review_status": "flagged_for_review" if flags else "not_assessed",
            "sensitivity_review_flags": flags, "external_processing_authorized": False,
            "preprocessing_flags": difficulty_flags(doc),
            "source_excel_rows": doc.source_rows, "original_metadata": doc.original_metadata,
        })
    # This invariant must hold before writing any pilot output.
    ownership: dict[str, set[str]] = {}
    for row in rows:
        ownership.setdefault(row["leakage_group_id"], set()).add(row["split"])
    if any(len(splits) != 1 for splits in ownership.values()) or len(rows) != len({row["document_id"] for row in rows}):
        raise RuntimeError("Internal pilot uniqueness or split-leakage validation failed.")
    summary: dict[str, Any] = {
        "seed": SEED, "target_documents": sum(TARGETS.values()), "selected_unique_documents": len(rows),
        "eligible_unique_documents": len(documents), "groups": group_summary,
        "sampling_group_counts": dict(Counter(row["sampling_group"] for row in rows)),
        "split_counts": dict(Counter(row["split"] for row in rows)),
        "random_corpus_sample_count": len(random_ids),
        "evaluation_related_nonrandom_documents": sum(row["split"] == "evaluation" and not row["is_random_corpus_sample"] for row in rows),
        "reserved_evaluation_group_ids": sorted(reserved),
        "reserved_evaluation_corpus_document_count": sum(len(groups.members[group]) for group in reserved),
        "selection_order": list(TARGETS), "shortage_fill_policy": "Fill only after all five primary strata; preserve the original random group identity.",
        "research_tag_rules": {"explicit_tags": sorted(RESEARCH_TAGS | set(extra_research_tags)),
                               "additional_tag_keyword_pattern": RESEARCH_PATTERN.pattern,
                               "matching_observed_tags": sorted(research_tags.values())},
        "existing_tags_are_ground_truth": False,
        "flagged_for_sensitivity_review": sum(bool(row["sensitivity_review_flags"]) for row in rows),
        "unflagged_sensitivity_status": "not_assessed; never presumed safe for external processing",
        "external_processing_authorized": False,
        "split_leakage_check_passed": True,
        "split_policy": "Every full-corpus hash/family/parent component containing a random-sample document is reserved for evaluation, including related nonrandom and unselected documents. All other components are development. Only the identifiable random group is eligible for unbiased metrics; random documents are not redrawn to alter group sizes.",
        "future_split_requirements": "Consult corpus_summary.json leakage_protection.groups and these reserved_evaluation_group_ids for all future selections. Recompute and audit reservations if new records connect previously separate components.",
        "limitations": "Protection covers reported valid hashes and explicit family/parent links only. Missing metadata cannot establish content uniqueness or absence of relationships. Multiple matching documents may remain inside the same split.",
    }
    return rows, summary
