"""Exit 0 if the Alembic revision given as argument has a real downgrade(), else 1.

A downgrade whose body is only a docstring, `pass`, `...` or a raise NotImplementedError is not
a real one (ENF-OPS-002: the migration line must not overstate reversibility)."""

import ast
import sys


def is_real_downgrade(source: str) -> bool:
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = [
                s
                for s in node.body
                if not (isinstance(s, ast.Pass))
                and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            ]
            if any(isinstance(s, ast.Raise) for s in body):
                return False
            return bool(body)
    return False


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        sys.exit(0 if is_real_downgrade(fh.read()) else 1)
