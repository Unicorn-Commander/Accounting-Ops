# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""Retained memberships must never silently become authorization again."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Keys identify ONE membership-reading statement within a function, not a file.
# The index counts membership statements only; stale exceptions fail below.
ALLOW_ALL_STATUSES = {
    ("app/security/member_removal.py", "remove_person", 0):
        "Removal locks the retained row and distinguishes absent/revoked/paused members.",
    ("app/api/members.py", "accept_invitation", 0):
        "An accepted invitation must inspect and restart a revoked row.",
    ("app/api/members.py", "list_members", 0):
        "The admin list deliberately includes paused members (but excludes revoked ones).",
    ("app/api/provisioning.py", "_seat", 0):
        "The flag-gated control plane needs the stored status to decide the next seat.",
    ("app/api/provisioning.py", "ensure_jit_membership", 0):
        "The flag-gated provisioning path locks any existing row before deciding its status.",
}
READ = re.compile(
    r"\b(?:from|join)\s+public\.workspace_membership\b"
    r"(?:\s+(?:as\s+)?(?P<alias>(?!where\b|join\b|on\b|order\b|limit\b)[a-z_]\w*))?",
    re.I,
)


def _literal(node):
    """Keep SQL literal parts, including concatenations and interpolated SQL."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal(value) or "{}" for value in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return (_literal(node.left) or "{}") + (_literal(node.right) or "{}")
    return None


def _statements(node, function="<module>"):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        function = node.name
    literal = _literal(node)
    if literal is not None:
        # Comments/docstrings mentioning FROM are not executable statements.
        if re.match(r"\s*(select|with|update|insert|delete)\b", literal, re.I):
            for sql in literal.split(";"):
                if re.search(r"\bselect\b", sql, re.I) and READ.search(sql):
                    yield function, node.lineno, sql
        return
    for child in ast.iter_child_nodes(node):
        yield from _statements(child, function)


def test_every_membership_reader_has_a_membership_status_condition():
    failures, seen_exceptions = [], set()
    for directory in ("app", "agents"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            indices = {}
            for function, line, sql in _statements(ast.parse(path.read_text())):
                index = indices.get(function, 0)
                indices[function] = index + 1
                key = (path.relative_to(ROOT).as_posix(), function, index)
                if key in ALLOW_ALL_STATUSES:
                    seen_exceptions.add(key)
                    continue
                for read in READ.finditer(sql):
                    alias = read.group("alias")
                    column = rf"\b{alias}\.status" if alias else r"(?<![\w.])(?:workspace_membership\.)?status"
                    # A SELECTed status or w.status predicate does not count.
                    if not re.search(column + r"\s*(?:=|!=|<>|\bin\b|\bis\b)", sql, re.I):
                        failures.append(f"{key[0]}:{line} {function}[{index}]")
    assert seen_exceptions == set(ALLOW_ALL_STATUSES), "Stale membership SQL allowlist"
    assert not failures, "Membership readers missing status conditions:\n" + "\n".join(failures)
