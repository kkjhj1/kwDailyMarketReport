"""CI test gate / Oracle deploy gate 구조 검증.

- test.yml 이 compileall → pytest → holdout_eval → audit_test_quality 게이트를 모두 실행하는지
- deploy_oracle.yml 이 raw push 없이 테스트 성공 이후에만 배포하는지, 원격 스크립트가 안전 장치를 갖췄는지
- auto_factory PRESETS 의 경로가 실제 디스크에 존재하는지
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _on(doc: dict):
    # PyYAML(YAML 1.1)은 `on` 키를 True 로 파싱한다.
    return doc.get("on") if "on" in doc else doc.get(True)


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def _remote_script(doc: dict) -> str:
    for step in doc["jobs"]["deploy"]["steps"]:
        if step.get("name") == "Execute Remote Deploy on Oracle VM":
            return step["with"]["script"]
    raise AssertionError("remote deploy step not found")


GATE_COMMANDS = (
    "python -m compileall -q",
    "python -m pytest -v",
    "python -m pytest holdout_eval -v",
    "python scripts/audit_test_quality.py",
)


# ---------------------------------------------------------------- test.yml
def test_test_workflow_runs_full_gate():
    doc = _load("test.yml")
    assert doc["name"] == "Python unit tests"
    on_cfg = _on(doc)
    assert "push" in on_cfg and "pull_request" in on_cfg
    run = _run_text(doc["jobs"]["tests"])
    assert "pyyaml" in run
    for cmd in GATE_COMMANDS:
        assert cmd in run, cmd
    positions = [run.index(cmd) for cmd in GATE_COMMANDS]
    assert positions == sorted(positions)


# ---------------------------------------------------------------- deploy_oracle.yml triggers
def test_deploy_has_no_raw_push_and_waits_for_tests():
    on_cfg = _on(_load("deploy_oracle.yml"))
    assert "push" not in on_cfg
    wr = on_cfg["workflow_run"]
    assert wr["workflows"] == ["Python unit tests"]
    assert wr["types"] == ["completed"]
    assert wr["branches"] == ["main"]
    assert "workflow_dispatch" in on_cfg


def test_deploy_dispatch_runs_tests_job_first():
    doc = _load("deploy_oracle.yml")
    tests_job = doc["jobs"]["tests"]
    assert "workflow_dispatch" in tests_job["if"]
    run = _run_text(tests_job)
    assert "pyyaml" in run
    for cmd in GATE_COMMANDS:
        assert cmd in run, cmd

    deploy = doc["jobs"]["deploy"]
    needs = deploy["needs"]
    assert "tests" in (needs if isinstance(needs, list) else [needs])
    cond = deploy["if"]
    assert "!cancelled()" in cond
    assert "workflow_run.conclusion == 'success'" in cond
    assert "needs.tests.result == 'success'" in cond


def test_deploy_concurrency_guard():
    conc = _load("deploy_oracle.yml")["concurrency"]
    assert conc["group"] == "deploy-oracle-cloud"
    assert conc["cancel-in-progress"] is True


# ---------------------------------------------------------------- remote script
def test_remote_script_safety_checks():
    doc = _load("deploy_oracle.yml")
    script = _remote_script(doc)
    step = next(s for s in doc["jobs"]["deploy"]["steps"] if s.get("name") == "Execute Remote Deploy on Oracle VM")
    assert "DEPLOY_SHA" in step["env"]
    assert "DEPLOY_SHA" in step["with"]["envs"]

    assert "set -Eeuo pipefail" in script
    assert "git diff --name-only -z HEAD -- ':!reports/'" in script
    assert "git ls-files -z --others --exclude-standard" in script and "\\.py$" in script
    assert 'git cat-file -e "${DEPLOY_SHA}^{commit}"' in script
    assert 'git merge-base --is-ancestor "$DEPLOY_SHA" origin/main' in script
    assert 'git reset --hard "$DEPLOY_SHA"' in script
    assert "git reset --hard origin/main" not in script


def test_remote_script_ordering_and_restore():
    script = _remote_script(_load("deploy_oracle.yml"))
    stop = script.index("sudo systemctl stop multibot")
    backup = script.index('cp -a reports "$BACKUP_DIR/reports"')
    reset = script.index('git reset --hard "$DEPLOY_SHA"')
    restore_reports = script.index('rm -rf reports && cp -a "$BACKUP_DIR/reports" reports')
    restore_db = script.index('"$BACKUP_DIR"/*.sqlite3*')
    start = script.index("sudo systemctl start multibot")
    assert stop < backup < reset < restore_reports < start
    assert reset < restore_db < start
    # 백업은 모든 *.sqlite3* 를 대상으로 한다.
    assert '"$APP_DIR"/*.sqlite3*' in script
    # 복원은 무조건 수행(파일 부재 조건 없이)한다.
    assert "! -f" not in script

    compile_pos = script.index("./venv/bin/python -m compileall -q")
    assert restore_db < compile_pos < start
    assert "sudo systemctl is-active --quiet multibot" in script
    assert "curl -fsS http://127.0.0.1:8002/healthz" in script
    assert "systemctl restart" not in script


# ---------------------------------------------------------------- presets
def test_presets_reference_existing_files():
    from auto_factory.presets import PRESETS

    assert PRESETS
    for alias, p in PRESETS.items():
        for rel in p.allowed_paths + p.tests:
            assert (ROOT / rel).is_file(), f"{alias}: missing {rel}"
        assert set(p.tests) <= set(p.allowed_paths), alias
