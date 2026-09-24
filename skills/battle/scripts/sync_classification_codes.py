#!/usr/bin/env python3
"""Validate and sync Battle classifications into Memory's shared catalog."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

CATALOG = Path(__file__).resolve().parents[1] / "fixtures" / "classification_codes.json"
COLLECTION = "classification_codes"


def load_documents(path: Path) -> tuple[str, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "battle.classification_code_catalog.v1":
        raise ValueError("invalid_catalog_schema")
    entries = data.get("codes")
    if not isinstance(entries, list) or not entries:
        raise ValueError("empty_catalog")
    by_code = {entry.get("code"): entry for entry in entries}
    if None in by_code or len(by_code) != len(entries):
        raise ValueError("missing_or_duplicate_code")
    catalog_sha = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    documents: list[dict[str, Any]] = []
    for entry in entries:
        code = entry["code"]
        parent = entry.get("parent_code")
        family_path = entry.get("family_path")
        description = entry.get("description")
        if parent is not None and parent not in by_code:
            raise ValueError(f"unknown_parent:{code}:{parent}")
        if not isinstance(family_path, list) or not family_path or not all(
            isinstance(part, str) and part for part in family_path
        ):
            raise ValueError(f"invalid_family_path:{code}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"missing_description:{code}")
        seen = {code}
        cursor = parent
        while cursor is not None:
            if cursor in seen:
                raise ValueError(f"parent_cycle:{code}")
            seen.add(cursor)
            cursor = by_code[cursor].get("parent_code")
        documents.append(
            {
                "_key": code,
                "schema": "memory.classification_code.v1",
                "code": code,
                "description": description,
                "cause": description,
                "layer": "battle",
                "kind": entry.get("kind", "classification"),
                "family_path": family_path,
                "parent_code": parent,
                "parent_family": parent,
                "catalog_owner": "battle",
                "catalog_sha256": catalog_sha,
                "tags": [
                    "classification-code",
                    "battle",
                    entry.get("kind", "classification"),
                    *[f"family:{part}" for part in family_path],
                ],
                "retrieval_text": " ".join(
                    [code, description, entry.get("kind", "classification"), *family_path]
                ),
            }
        )
    return catalog_sha, documents


def sync(*, catalog: Path, base_url: str) -> dict[str, Any]:
    catalog_sha, documents = load_documents(catalog)
    with httpx.Client(
        base_url=base_url, timeout=httpx.Timeout(30.0, connect=2.0)
    ) as client:
        for offset in range(0, len(documents), 10):
            response = client.post(
                "/upsert",
                json={"collection": COLLECTION, "documents": documents[offset : offset + 10]},
            )
            response.raise_for_status()
        readback = client.post(
            "/list",
            json={
                "collection": COLLECTION,
                "filters": {"catalog_owner": "battle", "catalog_sha256": catalog_sha},
                "limit": len(documents) + 1,
            },
        )
        readback.raise_for_status()
        returned = readback.json().get("documents", [])
    expected = {document["_key"]: document for document in documents}
    actual = {document.get("_key"): document for document in returned}
    if set(actual) != set(expected):
        raise ValueError(
            "readback_key_mismatch:"
            f"missing={sorted(set(expected) - set(actual))}:"
            f"unexpected={sorted(set(actual) - set(expected))}"
        )
    for code, document in expected.items():
        observed = actual[code]
        for field in (
            "schema",
            "code",
            "description",
            "family_path",
            "parent_code",
            "parent_family",
            "kind",
            "catalog_owner",
            "catalog_sha256",
        ):
            if observed.get(field) != document.get(field):
                raise ValueError(f"readback_field_mismatch:{code}:{field}")
    return {
        "schema": "battle.classification_code_memory_sync.v1",
        "status": "PASS",
        "collection": COLLECTION,
        "catalog_sha256": catalog_sha,
        "stored": len(documents),
        "read_back": len(actual),
        "root_code": "BATTLE",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--base-url", default="http://127.0.0.1:8601")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipt = sync(catalog=args.catalog, base_url=args.base_url)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
