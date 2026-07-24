"""Regression tests for external activity routing in the track endpoint."""

from __future__ import annotations

import ast
from pathlib import Path

source_path = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "__init__.py"
)
tree = ast.parse(source_path.read_text(encoding="utf-8"))

target = None
for node in ast.walk(tree):
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_is_standalone_external_activity"
    ):
        target = node
        break

assert target is not None
module = ast.Module(body=[target], type_ignores=[])
namespace = {}
exec(compile(module, str(source_path), "exec"), namespace)
is_external = namespace["_is_standalone_external_activity"]


def test_synthetic_komoot_id_is_external():
    assert is_external({"id": "komoot:abc"})


def test_komoot_source_is_external_even_with_an_unusual_id():
    assert is_external({"id": "external-1", "source": "komoot_gpx"})


def test_real_bosch_activity_is_not_external():
    assert not is_external(
        {
            "id": "3978f580-8055-11f1-8c92-00075fe0408d",
            "source": "bosch",
        }
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
