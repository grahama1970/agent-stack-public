from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "contact_sheet.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("contact_sheet_catalog", SCRIPT)
assert spec and spec.loader
catalog_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog_module)

AssetCatalogError = catalog_module.AssetCatalogError
compile_asset_bindings = catalog_module.compile_asset_bindings
project_asset_catalog = catalog_module.project_asset_catalog


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qualified_catalog(tmp_path: Path) -> tuple[dict, Path, Path]:
    media = tmp_path / "embry.png"
    media.write_bytes(b"retained canonical media")
    qualification = tmp_path / "qualification.json"
    qualification.write_text(json.dumps({
        "schema": "persona_dream.contact_sheet_identity_qualification.v1",
        "status": "PASS_CONTACT_SHEET_IDENTITY_QUALIFIED",
        "asset_id": "embry-v3",
        "image": {"sha256": f"sha256:{sha(media)}"},
    }))
    manifest = {
        "schema": "persona_dream.reference_asset_manifest.v1",
        "root": str(tmp_path),
        "assets": [{
            "_key": "canonical-record-1",
            "asset_id": "embry-v3",
            "sha256": sha(media),
            "image_path": str(media),
            "media_kind": "image",
            "persona_scope": ["embry"],
            "project_scope": ["persona-dream"],
            "provenance": {"image_path": str(media), "canonical_record_key": "canonical-record-1"},
            "qualification_evidence": [{"receipt": str(qualification), "sha256": sha(qualification)}],
            "lifecycle_status": "qualified",
            "reuse_restrictions": ["synthetic-reference-only"],
            "reference_roles": ["identity"],
        }],
    }
    manifest_path = tmp_path / "reference_asset_manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return project_asset_catalog(manifest, manifest_path), manifest_path, media


def request(run_id: str = "run-a") -> dict:
    return {
        "run_id": run_id,
        "persona": "embry",
        "project": "persona-dream",
        "scene_id": "scene-1",
        "required_duties": [{"duty": "identity", "canonical_id": "embry-v3"}],
    }


def bind(catalog: dict, run_id: str = "run-a") -> dict:
    binding_request = request(run_id)
    binding_request["required_duties"][0]["version_content_hash"] = catalog["entries"][0]["version_content_hash"]
    return compile_asset_bindings(catalog, **binding_request)


def test_one_canonical_asset_binds_to_two_runs_without_duplication(tmp_path: Path) -> None:
    catalog, manifest, _ = qualified_catalog(tmp_path)
    first, second = bind(catalog, "run-a"), bind(catalog, "run-b")
    assert first["bindings"][0]["compiler_input"] == second["bindings"][0]["compiler_input"]
    assert first["canonical_manifest_sha256"] == sha(manifest)
    assert first["media_duplicated"] is second["media_duplicated"] is False
    assert first["approved"] is first["accepted"] is False


def test_tampered_projection_is_typed(tmp_path: Path) -> None:
    catalog, _, _ = qualified_catalog(tmp_path)
    catalog["entries"][0]["persona_scope"] = ["attacker"]
    with pytest.raises(AssetCatalogError) as caught:
        bind(catalog)
    assert caught.value.code == "CANONICAL_MANIFEST_MISMATCH"


def test_missing_qualification_is_typed(tmp_path: Path) -> None:
    catalog, manifest_path, _ = qualified_catalog(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["assets"][0]["qualification_evidence"] = []
    manifest["assets"][0]["lifecycle_status"] = "unqualified"
    manifest_path.write_text(json.dumps(manifest))
    catalog = project_asset_catalog(manifest, manifest_path)
    with pytest.raises(AssetCatalogError) as caught:
        bind(catalog)
    assert caught.value.code == "MISSING_ASSET_QUALIFICATION"


def test_hash_valid_but_nonqualifying_receipt_is_typed(tmp_path: Path) -> None:
    catalog, manifest_path, _ = qualified_catalog(tmp_path)
    nonqualifying = tmp_path / "nonqualifying.json"
    nonqualifying.write_text("{}")
    manifest = json.loads(manifest_path.read_text())
    manifest["assets"][0]["qualification_evidence"] = [{"receipt": str(nonqualifying), "sha256": sha(nonqualifying)}]
    manifest_path.write_text(json.dumps(manifest))
    catalog = project_asset_catalog(manifest, manifest_path)
    with pytest.raises(AssetCatalogError) as caught:
        bind(catalog)
    assert caught.value.code == "QUALIFICATION_RECEIPT_NONQUALIFYING"


def test_qualification_identity_mismatch_is_typed(tmp_path: Path) -> None:
    catalog, manifest_path, _ = qualified_catalog(tmp_path)
    receipt_path = tmp_path / "qualification.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["asset_id"] = "different-asset"
    receipt_path.write_text(json.dumps(receipt))
    manifest = json.loads(manifest_path.read_text())
    manifest["assets"][0]["qualification_evidence"][0]["sha256"] = sha(receipt_path)
    manifest_path.write_text(json.dumps(manifest))
    catalog = project_asset_catalog(manifest, manifest_path)
    with pytest.raises(AssetCatalogError) as caught:
        bind(catalog)
    assert caught.value.code == "QUALIFICATION_IDENTITY_MISMATCH"


def test_forged_or_missing_qualification_receipt_is_typed(tmp_path: Path) -> None:
    catalog, manifest_path, _ = qualified_catalog(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["assets"][0]["qualification_evidence"] = [{"receipt": str(tmp_path / "missing.json"), "sha256": "0" * 64}]
    manifest_path.write_text(json.dumps(manifest))
    catalog = project_asset_catalog(manifest, manifest_path)
    with pytest.raises(AssetCatalogError) as caught:
        bind(catalog)
    assert caught.value.code == "QUALIFICATION_RECEIPT_INVALID"


def test_substituted_media_bytes_are_typed(tmp_path: Path) -> None:
    catalog, _, media = qualified_catalog(tmp_path)
    media.write_bytes(b"substituted bytes")
    with pytest.raises(AssetCatalogError) as caught:
        bind(catalog)
    assert caught.value.code == "MEDIA_HASH_MISMATCH"


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda c, r: r["required_duties"][0].update(version_content_hash="stale"), "STALE_ASSET_HASH"),
    (lambda c, r: r.update(persona="horus"), "WRONG_ASSET_SCOPE"),
])
def test_binding_negative_cases_are_typed(tmp_path: Path, mutate, code: str) -> None:
    catalog, _, _ = qualified_catalog(tmp_path)
    binding_request = request()
    binding_request["required_duties"][0]["version_content_hash"] = catalog["entries"][0]["version_content_hash"]
    mutate(catalog, binding_request)
    with pytest.raises(AssetCatalogError) as caught:
        compile_asset_bindings(catalog, **binding_request)
    assert caught.value.code == code


def test_no_required_duties_blocks_compilation(tmp_path: Path) -> None:
    catalog, _, _ = qualified_catalog(tmp_path)
    binding_request = request()
    binding_request["required_duties"] = []
    with pytest.raises(AssetCatalogError) as caught:
        compile_asset_bindings(catalog, **binding_request)
    assert caught.value.code == "UNRESOLVED_REQUIRED_DUTY"


def test_conflicting_duties_block_compilation(tmp_path: Path) -> None:
    catalog, manifest_path, _ = qualified_catalog(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["assets"][0]["reference_roles"] = ["identity", "composition"]
    manifest_path.write_text(json.dumps(manifest))
    catalog = project_asset_catalog(manifest, manifest_path)
    binding_request = request()
    asset_hash = catalog["entries"][0]["version_content_hash"]
    binding_request["required_duties"][0]["version_content_hash"] = asset_hash
    binding_request["required_duties"].append({"duty": "composition", "canonical_id": "embry-v3", "version_content_hash": asset_hash})
    with pytest.raises(AssetCatalogError) as caught:
        compile_asset_bindings(catalog, **binding_request)
    assert caught.value.code == "CONFLICTING_REFERENCE_DUTIES"
