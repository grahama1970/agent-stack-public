from battle_skill.adaptive_invariant_battle import (
    _ContextualOracle,
    _functional_control_passed,
    _is_red_win,
    _score,
)
from battle_skill.adaptive_lineage_engine import (
    GenerationRequest,
    JudgedPopulation,
    PopulationStageResult,
    RecallResult,
)


def test_red_win_dominates_mutation_complexity() -> None:
    simple_win = _score(
        {"verdict": "FAIL", "execution": {"kind": "ACCEPT"},
         "security_judge": {"status": "PASS"}},
        {"hop_count": 2, "clues": ["a", "b"], "carriers": ["txt"]},
    )
    complex_defended = _score(
        {"verdict": "PASS", "execution": {"kind": "CONTRACT_REJECT"},
         "security_judge": {"status": "PASS"}},
        {"hop_count": 99, "clues": list("abcdef"), "ambiguous": True,
         "carriers": ["txt", "json", "png"]},
    )
    assert simple_win > complex_defended


def test_independent_judge_failure_is_red_win_signal() -> None:
    score = _score(
        {"verdict": "FAIL", "execution": {"kind": "ACCEPT"},
         "security_judge": {"status": "FAIL"}},
        {"hop_count": 2, "clues": ["a", "b"], "carriers": ["txt"]},
    )
    assert score[:3] == [1, 1, 1]


def test_launch_error_is_not_a_red_win() -> None:
    receipt = {
        "verdict": "FAIL", "execution": {"kind": "LAUNCH_ERROR", "exit_code": 125},
        "security_judge": {"status": "PASS"},
    }
    mutation = {"hop_count": 99, "clues": ["a", "b"], "carriers": ["txt"]}
    assert _is_red_win(receipt) is False
    assert _score(receipt, mutation)[0] == 0


def test_functional_control_requires_accept_and_independent_judge_pass() -> None:
    assert _functional_control_passed(
        {
            "verdict": "PASS",
            "execution": {"kind": "ACCEPT", "exit_code": 0},
            "functional_judge": {"status": "PASS"},
        }
    )
    assert not _functional_control_passed(
        {
            "verdict": "PASS",
            "execution": {"kind": "CONTRACT_REJECT", "exit_code": 2},
            "functional_judge": {"status": "NOT_APPLICABLE"},
        }
    )


def test_contextual_oracle_selects_by_objective_judge_score(tmp_path) -> None:
    defended = {
        "candidate_id": "defended", "score": [0, 1, 1, 99],
        "judge_verdict": "DEFENDED", "case_receipt_sha256": "sha256:defended",
    }
    red_win = {
        "candidate_id": "red-win", "score": [1, 1, 1, 2],
        "judge_verdict": "RED_SUCCESS", "case_receipt_sha256": "sha256:red",
    }
    population = PopulationStageResult(items=(defended, red_win))
    selection = _ContextualOracle().select(
        JudgedPopulation(population, population, population),
        request=GenerationRequest(
            battle_id="battle", lineage_id="lineage", run_id="run", role="red",
            generation=1, population_size=2, max_generations=1,
            max_recall_hops=2, out_dir=tmp_path, subgraph_scope={"role": "red"},
        ),
        recall=RecallResult(receipt={"status": "PASS"}),
    )
    assert selection.survivor["candidate_id"] == "red-win"
    assert selection.evidence["source"] == "independent_invariant_judge_case_receipts"
    assert selection.evidence["candidate_scores"][1]["case_receipt_sha256"] == "sha256:red"
