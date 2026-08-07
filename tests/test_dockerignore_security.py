"""Security regression tests for the Docker build context."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate the Docker glob subset used by this repository."""
    output: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern[index : index + 3] == "**/":
            output.append("(?:.*/)?")
            index += 3
        elif pattern[index : index + 2] == "**":
            output.append(".*")
            index += 2
        elif pattern[index] == "*":
            output.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            output.append("[^/]")
            index += 1
        else:
            output.append(re.escape(pattern[index]))
            index += 1

    body = "".join(output)
    if "/" not in pattern:
        return re.compile(rf"(?:^|/){body}(?:$|/)")
    return re.compile(rf"^{body}(?:$|/)")


def _is_excluded(path: str) -> bool:
    excluded = False
    for raw_line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        pattern = pattern.strip("/")
        if _glob_regex(pattern).search(path.strip("/")):
            excluded = not negated
    return excluded


def test_high_risk_local_files_are_excluded_from_build_context() -> None:
    high_risk_paths = (
        ".env",
        ".env.local",
        ".env.production",
        "services/router/.env.staging",
        ".envrc",
        "config/providers.yaml",
        ".bedrock_agentcore.yaml",
        ".bedrock_agentcore/runtime/state.json",
        "services/router/.bedrock_agentcore.json",
        ".aws/credentials",
        ".ssh/id_ed25519",
        "certs/client.key",
        ".venv/bin/python",
        "infra/.venv/lib/python/site.py",
        "venv/bin/activate",
        "src/gateway/__pycache__/agent.cpython-312.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".hypothesis/examples/example",
        ".mypy_cache/3.12/cache.json",
        ".ruff_cache/content",
        ".coverage",
        "coverage.xml",
        "htmlcov/index.html",
        "cdk.out/manifest.json",
        "infra/cdk.out/asset.123/app.py",
        "infra/cdk.context.json",
        ".git/config",
        ".idea/workspace.xml",
        ".vscode/settings.json",
        "dist/axonllm.whl",
        "build/lib/gateway.py",
        "node_modules/package/index.js",
        "gateway.log",
        "local.sqlite3",
    )

    leaked = [path for path in high_risk_paths if not _is_excluded(path)]
    assert leaked == [], f"high-risk paths included in Docker context: {leaked}"


def test_public_templates_and_runtime_source_remain_in_build_context() -> None:
    included_paths = (
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.production.example",
        "config/providers.yaml.example",
        "config/spokes.yaml.example",
        "config/models.yaml",
        "Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "src/gateway/agent.py",
        "docs/FEATURES_AND_FLOWS.md",
        "scripts/generate_openapi.py",
        "site/index.html",
    )

    unexpectedly_excluded = [path for path in included_paths if _is_excluded(path)]
    assert unexpectedly_excluded == [], (
        f"public templates or runtime source excluded: {unexpectedly_excluded}"
    )
