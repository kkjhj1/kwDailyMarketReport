"""Holdout invariants for auto_factory guardrails (run standalone: `pytest holdout_eval`)."""
from auto_factory.adapters import HandoffContract, ContractTooLarge, route_writer, select_reviewer
from auto_factory.guardrails import OscillationDetector, compare_sources, is_forbidden
from auto_factory.win_job import build_sandbox_env

BASE_TEST = "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n"


def test_forbidden_paths_stay_forbidden():
    for path in (".env", "sub/.env", "data/app.sqlite3", "reports/index.html", "deploy_to_oracle.ps1",
                 "holdout_eval/test_x.py", "pytest.ini", "tests/conftest.py"):
        assert is_forbidden(path), path
    assert not is_forbidden("auto_factory/worker.py")


def test_anti_cheating_detects_weakened_tests():
    path = "tests/test_x.py"
    skipped = "import pytest\n\n@pytest.mark.skip\n" + BASE_TEST
    weaker = "def test_a():\n    assert 1 == 1\n"
    assert {v.code for v in compare_sources({path: BASE_TEST}, {path: skipped})} == {"SKIP_ADDED"}
    assert {v.code for v in compare_sources({path: BASE_TEST}, {path: weaker})} == {"ASSERT_REDUCED"}
    assert {v.code for v in compare_sources({path: BASE_TEST}, {path: None})} == {"TEST_FILE_DELETED"}
    assert compare_sources({path: BASE_TEST}, {path: BASE_TEST}) == []


def test_routing_invariants():
    assert route_writer("L3", ["claude", "codex"]).outcome == "BLOCKED"
    assert route_writer("L1", ["gemini"]).outcome == "BLOCKED"
    for coder in ("claude", "codex", "gemini"):
        assert select_reviewer(coder, ["claude", "codex", "gemini"]) != coder


def test_oscillation_aborts_on_repeat():
    det = OscillationDetector()
    assert det.record("a", 2) == "RETRY"
    assert det.record("a", 2) == "ABORT_DUPLICATE"


def test_sandbox_env_disables_telegram():
    env = build_sandbox_env(base={"PATH": "x", "TELEGRAM_BOT_TOKEN": "123:abc"})
    assert env["TELEGRAM_BOT_TOKEN"] == ""


def test_handoff_contract_is_bounded():
    c = HandoffContract("t", "L1", "a" * 40, "b" * 40, ["x" * 3000], [], "writer")
    try:
        c.to_json()
    except ContractTooLarge:
        pass
    else:
        raise AssertionError("oversized contract accepted")
