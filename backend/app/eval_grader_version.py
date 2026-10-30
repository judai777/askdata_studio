"""确定性评测判分器的实现指纹。"""
from __future__ import annotations

import ast
import hashlib

import sqlglot

from .config import BASE_DIR


GRADE_FUNCTIONS = ("_normalize", "_rows_equal", "grade", "combine_attempts")


def grader_hash() -> str:
    source = (BASE_DIR / "app" / "evaluation.py").read_text(encoding="utf-8")
    functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    digest = hashlib.sha256()
    for name in GRADE_FUNCTIONS:
        snippet = ast.get_source_segment(source, functions[name])
        if snippet is None:
            raise ValueError(f"无法读取判分函数源码：{name}")
        digest.update(name.encode("utf-8"))
        digest.update(snippet.encode("utf-8"))
    digest.update(f"sqlglot:{sqlglot.__version__}".encode("utf-8"))
    return digest.hexdigest()[:16]
