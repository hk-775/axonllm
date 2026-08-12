"""Clean-wheel checks for installed AxonLLM launchers and resources."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import zipfile


_REPO = Path(__file__).resolve().parents[2]


def _build_wheel(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "LICENSE"):
        shutil.copy2(_REPO / name, project / name)
    shutil.copytree(
        _REPO / "src",
        project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    output = tmp_path / "dist"
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--offline",
            "--no-build-isolation",
            "--out-dir",
            str(output),
        ],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_clean_wheel_launchers_and_assets_work_outside_repository(tmp_path):
    wheel = _build_wheel(tmp_path)
    target = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        archive.extractall(target)

    entry_points_name = next(
        name for name in names if name.endswith(".dist-info/entry_points.txt")
    )
    with zipfile.ZipFile(wheel) as archive:
        entry_points = archive.read(entry_points_name).decode("utf-8")
    assert "axon = src.gateway.cli:main" in entry_points
    assert "axon-demo = src.gateway.cli:demo" in entry_points
    assert (
        "axon-agentcore-deploy = "
        "src.gateway.deployment.agentcore_deploy:main"
    ) in entry_points

    required = {
        "src/gateway/admin/static/index.html",
        "src/gateway/chat/static/index.html",
        "src/gateway/resources/runtime/config/models.yaml",
        "src/gateway/resources/runtime/config/pricing.yaml",
        "src/gateway/resources/runtime/site/index.html",
        "src/gateway/resources/runtime/site/axonllm-demo.mp4",
        "src/gateway/deployment/infra/app.py",
        "src/gateway/deployment/infra/cdk.json",
        "src/gateway/deployment/infra/requirements.txt",
    }
    assert required <= names
    assert "serve_dashboard.py" not in names
    assert "scripts/demo.sh" not in names
    assert "deploy-agentcore.sh" not in names

    outside = tmp_path / "outside"
    outside.mkdir()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(target)
    environment["AXON_LOAD_DEMO_DATA"] = "true"
    environment["AXON_NO_BROWSER"] = "true"
    environment["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    smoke = textwrap.dedent(
        f"""
        import json
        from pathlib import Path
        import sys

        from src.gateway import cli
        from src.gateway import local_demo
        from src.gateway import local_server
        from src.gateway.deployment import agentcore_deploy

        target = Path({str(target)!r}).resolve()
        assert target in Path(cli.__file__).resolve().parents

        calls = []
        cli.os.execv = lambda executable, argv: calls.append(
            (executable, argv)
        )
        sys.argv = ["axon", "serve", "--no-demo-data"]
        cli.main()
        cli.demo()
        assert calls[0][1][-1] == "src.gateway.local_server"
        assert calls[1][1][-1] == "src.gateway.local_demo"
        assert Path(local_demo.__file__).is_file()

        app, config = local_server.build_app()
        assert Path(config.models_config_path).is_file()
        assert Path(config.pricing_config_path).is_file()
        assert (local_server._RUNTIME_ROOT / "site" / "index.html").is_file()

        agentcore_deploy.INFRA_ROOT = Path({str(tmp_path / "cache-infra")!r})
        agentcore_deploy._materialize_infra()
        assert (agentcore_deploy.INFRA_ROOT / "app.py").is_file()
        assert (agentcore_deploy.INFRA_ROOT / "cdk.json").is_file()
        print(json.dumps({{"routes": len(app.routes)}}))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", smoke],
        cwd=outside,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"routes":' in completed.stdout

    help_result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.gateway.deployment.agentcore_deploy "
                "import main; main()"
            ),
            "--help",
        ],
        cwd=outside,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Deploy a validated AxonLLM AgentCore setup" in help_result.stdout
