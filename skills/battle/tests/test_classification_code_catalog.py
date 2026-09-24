"""Battle classification catalog hierarchy checks."""

from pathlib import Path

from battle_skill.config import SKILL_DIR

import importlib.util


SCRIPT = SKILL_DIR / "scripts" / "sync_classification_codes.py"
spec = importlib.util.spec_from_file_location("battle_sync_codes", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_catalog_is_complete_tree() -> None:
    digest, documents = module.load_documents(
        SKILL_DIR / "fixtures" / "classification_codes.json"
    )
    by_code = {document["code"]: document for document in documents}
    assert digest.startswith("sha256:")
    assert by_code["BATTLE"]["parent_code"] is None
    assert {
        "BATTLE_OBSERVATION",
        "BATTLE_RED",
        "BATTLE_BLUE",
        "BATTLE_JUDGE",
        "BATTLE_RUNTIME",
        "BATTLE_LINEAGE",
        "BATTLE_CAMPAIGN",
    } <= {code for code, item in by_code.items() if item["parent_code"] == "BATTLE"}
    assert all(item["description"] for item in documents)
    assert all(item["family_path"][0] == "battle" for item in documents)
