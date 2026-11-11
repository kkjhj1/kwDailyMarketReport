"""Zero-Token 정적 테스트 품질 검사기.

tests/test_*.py 를 AST로 분석해 다음 냄새를 색출한다.
  - CONST_ASSERT : assert True / assert False / assert 1 등 상수 assertion
  - NO_ASSERT    : assert 구문도, pytest.raises / pytest.fail 등 검증 호출도 없는 테스트
  - PASS_ONLY    : 본문이 pass / docstring / ... 뿐인 빈 테스트

사용법:
    python scripts/audit_test_quality.py [경로 ...]
    (기본 경로: tests/)

종료 코드: 위반 없음 0, 위반 있음 1
의도된 예외는 해당 def 줄에 `# audit: allow` 주석으로 허용한다.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARKER = "# audit: allow"

# assert 대신 검증 역할을 하는 호출 (속성명 또는 함수명 기준)
VERIFYING_CALLS = {
    "raises", "warns", "fail", "deprecated_call", "approx",
    "assert_called", "assert_called_once", "assert_called_with",
    "assert_called_once_with", "assert_any_call", "assert_has_calls",
    "assert_not_called", "assert_awaited", "assert_awaited_once",
    "assert_awaited_with", "assert_awaited_once_with", "assert_not_awaited",
}


@dataclass
class Violation:
    path: Path
    line: int
    test: str
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.code}] {self.test} - {self.detail}"


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_trivial_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    # docstring 또는 ... (Ellipsis)
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)


def _is_verifying(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Call):
        # assert_* / _assert_* 형태의 검증 헬퍼도 검증 호출로 인정
        name = _call_name(node).lstrip("_")
        return name in VERIFYING_CALLS or name.startswith("assert")
    if isinstance(node, ast.Raise):
        return True
    return False


def _walk_own_body(func: ast.AST):
    """중첩 함수/클래스 정의 내부는 제외하고 순회한다."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def audit_function(path: Path, func: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> list[Violation]:
    violations: list[Violation] = []

    if all(_is_trivial_stmt(s) for s in func.body):
        violations.append(Violation(path, func.lineno, name, "PASS_ONLY", "본문이 비어 있음(pass/docstring/...)"))
        return violations

    has_verification = False
    for node in _walk_own_body(func):
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant):
            violations.append(Violation(
                path, node.lineno, name, "CONST_ASSERT",
                f"상수 assertion: assert {node.test.value!r}",
            ))
        if _is_verifying(node):
            has_verification = True

    if not has_verification:
        violations.append(Violation(path, func.lineno, name, "NO_ASSERT", "assert/검증 호출이 1개도 없음"))
    return violations


def _iter_tests(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node, node.name
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    yield item, f"{node.name}.{item.name}"


def audit_file(path: Path) -> list[Violation]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, "<module>", "SYNTAX_ERROR", str(exc.msg))]

    lines = source.splitlines()
    violations: list[Violation] = []
    for func, name in _iter_tests(tree):
        def_line = lines[func.lineno - 1] if func.lineno - 1 < len(lines) else ""
        if ALLOW_MARKER in def_line:
            continue
        violations.extend(audit_function(path, func, name))
    return violations


def collect_files(targets: list[str]) -> list[Path]:
    """대상 경로에서 테스트 파일을 수집한다.

    겹치는 대상(같은 디렉터리 중복 지정, 디렉터리 + 그 안의 파일, 상대/절대 경로 혼용 등)으로
    동일 파일이 여러 번 수집되면 위반이 중복 보고되므로, 실제 경로(resolve) 기준으로 1회만 남기고
    결정적(sorted) 순서로 반환한다.
    """
    seen: dict[Path, Path] = {}
    for target in targets:
        p = Path(target)
        if p.is_dir():
            candidates = p.rglob("test_*.py")
        elif p.is_file():
            candidates = [p]
        else:
            continue
        for c in candidates:
            key = c.resolve()
            if key not in seen:
                seen[key] = c
    return [seen[key] for key in sorted(seen)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="정적 테스트 품질 검사기 (Zero-Token)")
    parser.add_argument("paths", nargs="*", default=["tests"], help="검사 대상 디렉터리/파일 (기본: tests)")
    args = parser.parse_args(argv)

    files = collect_files(args.paths)
    if not files:
        print(f"[AUDIT] 검사 대상 테스트 파일 없음: {args.paths}")
        return 1

    violations: list[Violation] = []
    test_count = 0
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            test_count += sum(1 for _ in _iter_tests(tree))
        except SyntaxError:
            pass
        violations.extend(audit_file(f))

    for v in violations:
        print(v)

    status = "FAIL" if violations else "PASS"
    print(f"[AUDIT] {status}: files={len(files)} tests={test_count} violations={len(violations)}")
    return 1 if violations else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
