"""Deterministic packaging contracts for the serverless control plane."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from src.gateway.deployment.serverless_artifacts import (
    build_artifacts,
    lambda_artifact_entries,
    static_asset_entries,
    verify_artifacts,
)

_REPOSITORY = Path(__file__).resolve().parents[2]
_SCRIPT = (
    _REPOSITORY
    / "scripts/release/build_serverless_control_artifacts.py"
)
_REVISION = "1" * 40


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _miniature_repository(root: Path) -> tuple[Path, Path]:
    repository = root / "repository"
    dependencies = root / "dependencies"
    _write(repository / "axonllm/__init__.py", "")
    _write(repository / "axonllm/py.typed", "")
    _write(repository / "src/__init__.py", "")
    _write(repository / "src/gateway/__init__.py", "")
    _write(
        repository / "src/gateway/serverless_control.py",
        "def lambda_handler(event, context):\n    return event\n",
    )
    _write(
        repository / "src/gateway/deployment/ignored.py",
        "raise RuntimeError('deployment code must not ship')\n",
    )
    _write(
        repository / "src/gateway/admin/static/index.html",
        "<!doctype html><title>AxonLLM</title>\n",
    )
    _write(
        repository / "src/gateway/admin/static/dashboard.js",
        "console.log('dashboard');\n",
    )
    _write(
        repository / "src/gateway/admin/static/tour/tour.json",
        "{}\n",
    )
    _write(repository / "site/architecture.html", "<h1>Architecture</h1>\n")
    _write(repository / "site/index.html", "<h1>Marketing</h1>\n")
    _write(repository / "site/deploy.sh", "#!/bin/sh\n")
    _write(repository / "site/infra/stack.py", "raise SystemExit\n")
    _write(repository / "site/narration/scene.json", "{}\n")
    _write(repository / "config/models.yaml", "models: []\n")
    _write(repository / "config/deployment/ignored.yaml", "version: 1\n")
    _write(repository / "docs/architecture.svg", "<svg/>\n")
    _write(dependencies / "mangum/__init__.py", "__version__ = 'test'\n")
    _write(dependencies / "certifi/cacert.pem", "PUBLIC CA CERTIFICATE\n")
    _write(dependencies / "bin/ignored", "#!/bin/sh\n")
    _write(dependencies / "__pycache__/ignored.pyc", "bytecode\n")
    return repository, dependencies


def test_real_static_asset_layout_matches_cloudfront_paths() -> None:
    entries = static_asset_entries(_REPOSITORY)
    names = {path.as_posix() for path in entries}

    assert "index.html" in names
    assert "admin/static/dashboard.js" in names
    assert "admin/static/vendor/react.production.min.js" in names
    assert "admin/static/tour/tour-narration.json" in names
    assert "architecture.html" in names
    assert "narration/architecture-narration.json" in names
    assert "deploy.sh" not in names
    assert "site/index.html" not in names
    assert not any(name.startswith("infra/") for name in names)


def test_lambda_artifact_contains_runtime_not_deployment_or_ui(
    tmp_path: Path,
) -> None:
    dependencies = tmp_path / "dependencies"
    _write(dependencies / "mangum/__init__.py", "")
    entries = lambda_artifact_entries(_REPOSITORY, dependencies)
    names = {path.as_posix() for path in entries}

    assert "src/gateway/serverless_control.py" in names
    assert "src/gateway/bootstrap.py" in names
    assert "config/models.yaml" in names
    assert "docs/architecture.svg" in names
    assert "mangum/__init__.py" in names
    assert not any(name.startswith("src/gateway/deployment/") for name in names)
    assert not any("/static/" in name for name in names)
    assert not any("__pycache__" in name for name in names)


def test_artifacts_and_receipt_are_byte_deterministic(tmp_path: Path) -> None:
    repository, dependencies = _miniature_repository(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_receipt = build_artifacts(
        repository,
        first,
        dependencies,
        _REVISION,
    )
    second_receipt = build_artifacts(
        repository,
        second,
        dependencies,
        _REVISION,
    )

    assert first_receipt == second_receipt
    assert (
        first / "serverless-control-artifacts.json"
    ).read_bytes() == (
        second / "serverless-control-artifacts.json"
    ).read_bytes()
    for metadata in (
        first_receipt.control_api,
        first_receipt.static_assets,
    ):
        first_archive = first / metadata.file_name
        second_archive = second / metadata.file_name
        assert first_archive.read_bytes() == second_archive.read_bytes()
        assert (
            hashlib.sha256(first_archive.read_bytes()).hexdigest()
            == metadata.sha256
        )
        assert metadata.sha256 in metadata.file_name
        with zipfile.ZipFile(first_archive) as archive:
            information = archive.infolist()
            assert [item.filename for item in information] == sorted(
                item.filename for item in information
            )
            assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in information)
            assert all(
                stat.S_IMODE(item.external_attr >> 16) == 0o644
                for item in information
            )

    static_names = set(
        zipfile.ZipFile(
            first / first_receipt.static_assets.file_name
        ).namelist()
    )
    assert static_names == {
        "admin/static/dashboard.js",
        "admin/static/tour/tour.json",
        "architecture.html",
        "index.html",
        "narration/scene.json",
    }
    control_names = set(
        zipfile.ZipFile(
            first / first_receipt.control_api.file_name
        ).namelist()
    )
    assert "src/gateway/serverless_control.py" in control_names
    assert "mangum/__init__.py" in control_names
    assert "certifi/cacert.pem" in control_names
    assert "bin/ignored" not in control_names
    assert "src/gateway/deployment/ignored.py" not in control_names

    receipt = json.loads(
        (first / "serverless-control-artifacts.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["schema"] == "axonllm.serverless-control-artifacts/v1"
    assert receipt["sourceRevision"] == _REVISION
    assert str(tmp_path) not in json.dumps(receipt)
    assert verify_artifacts(
        first,
        expected_source_revision=_REVISION,
    ) == first_receipt


def test_cli_runs_outside_repository(tmp_path: Path) -> None:
    repository, dependencies = _miniature_repository(tmp_path)
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    outside.mkdir()

    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--repository",
            str(repository),
            "--output-directory",
            str(output),
            "--dependency-root",
            str(dependencies),
            "--source-revision",
            _REVISION,
        ],
        cwd=outside,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["sourceRevision"] == _REVISION
    assert (output / "serverless-control-artifacts.json").is_file()


def test_invalid_revision_fails_before_writing(tmp_path: Path) -> None:
    repository, dependencies = _miniature_repository(tmp_path)
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="full lowercase Git commit"):
        build_artifacts(
            repository,
            output,
            dependencies,
            "not-a-commit",
        )

    assert not output.exists()


def test_verifier_rejects_tampered_artifact(tmp_path: Path) -> None:
    repository, dependencies = _miniature_repository(tmp_path)
    output = tmp_path / "output"
    receipt = build_artifacts(
        repository,
        output,
        dependencies,
        _REVISION,
    )
    archive = output / receipt.control_api.file_name
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="size does not match"):
        verify_artifacts(
            output,
            expected_source_revision=_REVISION,
        )


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows symlink creation requires elevated privileges",
)
def test_dependency_symlink_is_rejected(tmp_path: Path) -> None:
    repository, dependencies = _miniature_repository(tmp_path)
    target = dependencies / "mangum/__init__.py"
    link = dependencies / "linked.py"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic links"):
        lambda_artifact_entries(repository, dependencies)
