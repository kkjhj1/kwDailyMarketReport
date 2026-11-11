"""scripts/audit_test_quality.py 단위 테스트.

PASS_ONLY / NO_ASSERT / CONST_ASSERT / allow 마커 / 정상 테스트 판별을 검증한다.
검사 대상 소스는 tmp_path 에 임시 파일로 생성하므로 실제 tests/ 에는 영향이 없다.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "scripts" / "audit_test_quality.py"
_spec = importlib.util.spec_from_file_location("audit_test_quality", _SCRIPT)
aq = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("audit_test_quality", aq)
_spec.loader.exec_module(aq)


def _write(tmp_path: Path, source: str, name: str = "test_sample.py") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _codes(violations) -> list[str]:
    return [v.code for v in violations]


# ---------------------------------------------------------------- PASS_ONLY
@pytest.mark.parametrize(
    "body",
    ["pass", "...", '"""docstring only"""', '"""doc"""\n    pass'],
)
def test_pass_only_detected(tmp_path, body):
    path = _write(tmp_path, f"def test_empty():\n    {body}\n")
    violations = aq.audit_file(path)
    assert _codes(violations) == ["PASS_ONLY"]
    assert violations[0].test == "test_empty"
    assert violations[0].line == 1


# ---------------------------------------------------------------- NO_ASSERT
def test_no_assert_detected(tmp_path):
    path = _write(tmp_path, """
        def test_no_check():
            x = 1 + 1
            print(x)
    """)
    violations = aq.audit_file(path)
    assert _codes(violations) == ["NO_ASSERT"]
    assert violations[0].test == "test_no_check"


def test_no_assert_ignores_assert_in_nested_function(tmp_path):
    path = _write(tmp_path, """
        def test_nested_only():
            def helper():
                assert 1 == 1
            helper
    """)
    assert _codes(aq.audit_file(path)) == ["NO_ASSERT"]


def test_no_assert_in_test_class_method(tmp_path):
    path = _write(tmp_path, """
        class TestThing:
            def test_method(self):
                value = 3
    """)
    violations = aq.audit_file(path)
    assert _codes(violations) == ["NO_ASSERT"]
    assert violations[0].test == "TestThing.test_method"


# ---------------------------------------------------------------- CONST_ASSERT
@pytest.mark.parametrize("const", ["True", "False", "1", "'x'"])
def test_const_assert_detected(tmp_path, const):
    path = _write(tmp_path, f"def test_const():\n    x = 1\n    assert {const}\n")
    violations = aq.audit_file(path)
    assert _codes(violations) == ["CONST_ASSERT"]
    assert violations[0].line == 3


def test_const_assert_reported_alongside_real_assert(tmp_path):
    path = _write(tmp_path, """
        def test_mixed():
            assert 1 + 1 == 2
            assert True
    """)
    # 실제 assert 가 있으므로 NO_ASSERT 는 없고 CONST_ASSERT 만 보고된다.
    assert _codes(aq.audit_file(path)) == ["CONST_ASSERT"]


def test_only_const_assert_is_not_no_assert(tmp_path):
    path = _write(tmp_path, """
        def test_only_const():
            x = 1
            assert True
    """)
    codes = _codes(aq.audit_file(path))
    assert codes == ["CONST_ASSERT"]
    assert "NO_ASSERT" not in codes


# ---------------------------------------------------------------- allow marker
def test_allow_marker_skips_violation(tmp_path):
    path = _write(tmp_path, """
        def test_placeholder():  # audit: allow
            pass

        async def test_async_placeholder():  # audit: allow
            x = 1
    """)
    assert aq.audit_file(path) == []


def test_allow_marker_only_applies_to_marked_def(tmp_path):
    path = _write(tmp_path, """
        def test_allowed():  # audit: allow
            pass

        def test_not_allowed():
            pass
    """)
    violations = aq.audit_file(path)
    assert _codes(violations) == ["PASS_ONLY"]
    assert violations[0].test == "test_not_allowed"


# ---------------------------------------------------------------- valid tests
def test_valid_tests_have_no_violations(tmp_path):
    path = _write(tmp_path, """
        import pytest
        from unittest import mock

        def test_plain_assert():
            assert 1 + 1 == 2

        def test_pytest_raises():
            with pytest.raises(ValueError):
                int("x")

        def test_mock_verification():
            m = mock.Mock()
            m(1)
            m.assert_called_once_with(1)

        def _assert_helper(v):
            assert v

        def test_custom_assert_helper():
            _assert_helper(5)

        def test_explicit_raise():
            if 1 != 1:
                raise AssertionError("bad")

        async def test_async_assert():
            assert [1] == [1]

        class TestGroup:
            def test_in_class(self):
                assert "a".upper() == "A"

        def helper_not_a_test():
            pass

        class NotATestClass:
            def test_ignored(self):
                pass
    """)
    assert aq.audit_file(path) == []


def test_syntax_error_reported(tmp_path):
    path = _write(tmp_path, "def test_broken(:\n    pass\n")
    violations = aq.audit_file(path)
    assert _codes(violations) == ["SYNTAX_ERROR"]


# ---------------------------------------------------------------- main / CLI
def test_main_returns_zero_for_clean_dir(tmp_path, capsys):
    _write(tmp_path, "def test_ok():\n    assert 2 > 1\n")
    _write(tmp_path, "def helper():\n    pass\n", name="not_a_test.py")
    rc = aq.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[AUDIT] PASS" in out
    assert "tests=1" in out


def test_main_returns_one_with_violations(tmp_path, capsys):
    _write(tmp_path, "def test_bad():\n    pass\n")
    rc = aq.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[PASS_ONLY]" in out
    assert "violations=1" in out


def test_main_returns_one_when_no_files(tmp_path, capsys):
    rc = aq.main([str(tmp_path / "missing")])
    assert rc == 1
    assert "검사 대상 테스트 파일 없음" in capsys.readouterr().out
