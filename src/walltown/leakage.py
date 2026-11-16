"""Keep hash duplicates and transitive family relationships in the same split."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from walltown.manifest import Document


@dataclass
class LeakageGroups:
    document_groups: dict[str, str]
    members: dict[str, list[str]]
    hash_matches: dict[tuple[str, str], list[str]]


def build_leakage_groups(documents: dict[str, Document]) -> LeakageGroups:
    parents: dict[str, str] = {}

    def find(node: str) -> str:
        parents.setdefault(node, node)
        root = node
        while parents[root] != root:
            root = parents[root]
        while parents[node] != node:
            next_node = parents[node]
            parents[node] = root
            node = next_node
        return root

    def union(first: str, second: str) -> None:
        left, right = find(first), find(second)
        if left != right:
            parents[max(left, right)] = min(left, right)

    hashes: dict[tuple[str, str], list[str]] = defaultdict(list)
    referenced_ids = set(documents)
    for doc in documents.values():
        referenced_ids.update(doc.strings("parent_id"))
    for identifier, doc in sorted(documents.items()):
        node = "doc:" + identifier
        find(node)
        for parent in doc.strings("parent_id"):
            # Missing parents still connect their observed children to each other.
            union(node, "doc:" + parent)
        for family in doc.strings("family_id"):
            union(node, "family:" + family)
            if family in referenced_ids:
                union(node, "doc:" + family)
        for algorithm, values in doc.hashes.items():
            for value in values:
                hashes[(algorithm, value)].append(identifier)
                union(node, "hash:" + algorithm + ":" + value)
    components: dict[str, list[str]] = defaultdict(list)
    for identifier in sorted(documents):
        components[find("doc:" + identifier)].append(identifier)
    members: dict[str, list[str]] = {}
    document_groups: dict[str, str] = {}
    for ids in components.values():
        digest = hashlib.sha256("\0".join(ids).encode()).hexdigest()
        group_id = "group_" + digest
        members[group_id] = ids
        document_groups.update({identifier: group_id for identifier in ids})
    return LeakageGroups(document_groups, dict(sorted(members.items())), dict(hashes))
