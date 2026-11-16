from __future__ import annotations

import hashlib
import random

from walltown.features import discover_research_tags
from walltown.leakage import build_leakage_groups
from walltown.manifest import Document
from walltown.sampling import TARGETS, select_pilot


def document(identifier: str, tags: list[str] | None = None, **fields: object) -> Document:
    values = {"filename": ["same-name.pdf"], "page_count": [1], "file_size_bytes": [100],
              "file_type": ["application/pdf"], "md5": [hashlib.md5(identifier.encode()).hexdigest()]}
    values.update({key: value if isinstance(value, list) else [value] for key, value in fields.items()})
    return Document(identifier, values=values, tags=tags or [])


def test_transitive_hash_family_and_missing_parent_connections() -> None:
    docs = {
        "a": document("a", md5="a" * 32),
        "b": document("b", md5="a" * 32, parent_id="c"),
        "c": document("c", family_id="family"),
        "d": document("d", family_id="family"),
        "e": document("e", parent_id="absent-parent"),
        "f": document("f", parent_id="absent-parent"),
        "g": document("g"),
    }
    groups = build_leakage_groups(docs)
    assert len({groups.document_groups[key] for key in "abcd"}) == 1
    assert groups.document_groups["e"] == groups.document_groups["f"]
    assert groups.document_groups["g"] != groups.document_groups["a"]
    assert len(groups.members) == 3  # Identical filenames did not merge content.


def test_content_matches_are_compared_within_the_reported_algorithm() -> None:
    docs = {"a": document("a", md5="broken"), "b": document("b", md5="broken")}
    assert len(build_leakage_groups(docs).members) == 2
    docs["a"].values["hash"] = ["a" * 32]
    docs["b"].values["md5"] = ["a" * 32]
    assert len(build_leakage_groups(docs).members) == 2  # A generic hash algorithm is not guessed.


def test_full_targets_reproducibility_uniqueness_and_uniform_random_identity() -> None:
    docs = {}
    for index in range(1000):
        tags = ["IRRELEVANT"] if index < 500 else ["SOIL TESTING RESULTS"] if index < 800 else ["REC CENTER DEVELOPMENT"] if index < 950 else []
        identifier = f"d{index:04}"
        docs[identifier] = document(identifier, tags)
    groups = build_leakage_groups(docs)
    first, summary = select_pilot(docs, groups)
    second, second_summary = select_pilot(dict(reversed(list(docs.items()))), build_leakage_groups(docs))
    assert first == second
    assert summary == second_summary
    assert len(first) == len({row["document_id"] for row in first}) == 300
    assert summary["sampling_group_counts"] == TARGETS
    expected = set(random.Random(42).sample(sorted(docs), 100))
    assert {row["document_id"] for row in first if row["is_random_corpus_sample"]} == expected
    assert sum(row["include_in_unbiased_evaluation"] for row in first) == 100
    assert all(not row["external_processing_authorized"] for row in first)


def test_shortages_are_reported_and_filled_without_relabeling() -> None:
    docs = {str(index): document(str(index), ["GENERAL"]) for index in range(450)}
    rows, summary = select_pilot(docs, build_leakage_groups(docs))
    assert len(rows) == 300
    assert summary["sampling_group_counts"] == {"random_corpus": 100, "shortage_fill": 200}
    assert summary["groups"]["irrelevant_tag"]["shortage"] == 75
    assert sum(stats["shortage_filled"] for stats in summary["groups"].values()) == 200
    assert all(row["fills_shortage_for"] for row in rows if row["sampling_group"] == "shortage_fill")
    assert all(not row["is_random_corpus_sample"] for row in rows if row["sampling_group"] == "shortage_fill")


def test_small_corpus_never_repeats_documents() -> None:
    docs = {str(index): document(str(index)) for index in range(12)}
    rows, summary = select_pilot(docs, build_leakage_groups(docs))
    assert len(rows) == 12
    assert summary["random_corpus_sample_count"] == 12
    assert sum(stats["unfilled_shortage"] for stats in summary["groups"].values()) == 288


def test_known_related_documents_never_cross_splits_including_unselected_corpus_members() -> None:
    docs = {f"d{index:04}": document(f"d{index:04}", ["GENERAL"], family_id=f"family-{index // 5}") for index in range(600)}
    docs["d0000"].values["md5"] = docs["d0100"].values["md5"]
    groups = build_leakage_groups(docs)
    rows, summary = select_pilot(docs, groups)
    owners = {}
    for row in rows:
        owners.setdefault(row["leakage_group_id"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in owners.values())
    reserved = set(summary["reserved_evaluation_group_ids"])
    for row in rows:
        assert (row["split"] == "evaluation") == (row["leakage_group_id"] in reserved)
    assert summary["reserved_evaluation_corpus_document_count"] >= summary["random_corpus_sample_count"]
    assert summary["evaluation_related_nonrandom_documents"] > 0
    assert sum(row["include_in_unbiased_evaluation"] for row in rows) == 100


def test_a_giant_family_is_reserved_without_splitting_or_redrawing_random_sample() -> None:
    docs = {str(index): document(str(index), ["GENERAL"], family_id="one-family") for index in range(400)}
    rows, summary = select_pilot(docs, build_leakage_groups(docs))
    assert len(rows) == 300
    assert summary["split_counts"] == {"evaluation": 300}
    assert summary["reserved_evaluation_corpus_document_count"] == 400
    assert summary["random_corpus_sample_count"] == 100


def test_sensitivity_is_for_review_and_existing_tags_are_not_labels() -> None:
    docs = {"a": document("a", ["ATTORNEY CLIENT PRIVILEGED"]), "b": document("b", ["GENERAL"])}
    rows, summary = select_pilot(docs, build_leakage_groups(docs))
    by_id = {row["document_id"]: row for row in rows}
    assert by_id["a"]["sensitivity_review_status"] == "flagged_for_review"
    assert by_id["a"]["original_tags"] == ["ATTORNEY CLIENT PRIVILEGED"]
    assert by_id["b"]["sensitivity_review_status"] == "not_assessed"
    assert not summary["existing_tags_are_ground_truth"]


def test_additional_research_tags_are_discovered_by_transparent_rules() -> None:
    docs = {"a": document("a", ["GROUNDWATER CONTAMINATION"]), "b": document("b", ["CUSTOM RESEARCH"])}
    tags = discover_research_tags(docs, ("CUSTOM RESEARCH",))
    assert set(tags) == {"groundwater contamination", "custom research"}
