"""Production entrypoints must bind before running optional diagnostics."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS = (
    ROOT / "serve_dashboard.py",
    ROOT / "src/gateway/local_server.py",
)


def _main_function(path: Path) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )


def _main_calls(main: ast.FunctionDef) -> set[str]:
    calls: set[str] = set()
    for node in ast.walk(main):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.name)
def test_server_startup_never_runs_live_diagnostics(entrypoint: Path) -> None:
    source = entrypoint.read_text(encoding="utf-8")
    main = _main_function(entrypoint)
    calls = _main_calls(main)
    readiness_line = min(
        node.lineno
        for node in ast.walk(main)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "Readiness:" in node.value
    )
    uvicorn_line = next(
        node.lineno
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
        and node.func.attr == "run"
    )

    assert "run_checklist" not in source
    assert "load_provider_configs" not in source
    assert "HealthCheckTask" not in source
    assert "asyncio.run" not in source
    assert "run" in calls, "the entrypoint must still hand off to uvicorn"
    assert readiness_line < uvicorn_line


def test_image_verifier_starts_two_isolated_replicas() -> None:
    verifier = (ROOT / "scripts/ci/verify_image.py").read_text(encoding="utf-8")

    assert '"network", "create", "--internal"' in verifier
    assert verifier.count('"AXON_LOAD_DEMO_DATA=false"') == 1
    assert verifier.count('"AXON_CHECK_MODEL_AVAILABILITY=true"') == 1
    assert '"AWS_ACCESS_KEY_ID=ci-fake-access-key"' in verifier
    assert "_verify_two_replica_startup(image)" in verifier
    assert "timeout_seconds: float = 30.0" in verifier
    startup_verifier = verifier.split("def _verify_two_replica_startup", 1)[1].split(
        "def verify_image",
        1,
    )[0]
    assert '"--rm"' not in startup_verifier
    assert '"--publish"' not in startup_verifier
    assert '"docker", "exec", name, "python", "-c", _READINESS_PROBE' in verifier
