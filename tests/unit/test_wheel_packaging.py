"""Clean-wheel checks for installed AxonLLM launchers and resources."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
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
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    environment["UV_CACHE_DIR"] = str(cache)
    completed = subprocess.run(
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
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _install_wheel(wheel: Path, tmp_path: Path) -> tuple[Path, Path]:
    venv = tmp_path / "venv"
    completed = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    purelib = Path(
        subprocess.check_output(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
    )
    return python, purelib


def test_clean_wheel_launchers_and_assets_work_outside_repository(tmp_path):
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points_name = next(
            name
            for name in names
            if name.endswith(".dist-info/entry_points.txt")
        )
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
        "src/gateway/deployment/infra/app.py",
        "src/gateway/deployment/infra/cdk.json",
        "src/gateway/deployment/infra/requirements.txt",
    }
    required.update(
        path.relative_to(_REPO).as_posix()
        for path in (
            _REPO / "src" / "gateway" / "resources" / "runtime"
        ).rglob("*")
        if path.is_file()
    )
    assert required <= names
    assert "serve_dashboard.py" not in names
    assert "scripts/demo.sh" not in names
    assert "deploy-agentcore.sh" not in names

    python, purelib = _install_wheel(wheel, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    scripts = python.parent
    command_environment = os.environ.copy()
    command_environment.pop("PYTHONPATH", None)
    command_environment["PYTHONNOUSERSITE"] = "1"
    launcher_suffix = ".exe" if os.name == "nt" else ""
    for executable, expected in (
        (
            scripts / f"axon{launcher_suffix}",
            "The neural control plane",
        ),
        (
            scripts / f"axon-agentcore-deploy{launcher_suffix}",
            "Deploy a validated AxonLLM AgentCore setup",
        ),
    ):
        completed = subprocess.run(
            [str(executable), "--help"],
            cwd=outside,
            env=command_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert expected in completed.stdout

    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith(("AXON_", "LLM_ROUTER_")):
            environment.pop(name)
    host_package_paths = sorted(
        {
            path
            for path in (
                sysconfig.get_path("purelib"),
                sysconfig.get_path("platlib"),
            )
            if path
        }
    )
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(purelib),
            *host_package_paths,
        ]
    )
    environment["PYTHONNOUSERSITE"] = "1"
    environment["AXON_LOAD_DEMO_DATA"] = "true"
    environment["AXON_MODELS_CONFIG"] = "custom/models.yaml"
    environment["AXON_NO_BROWSER"] = "true"
    environment["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    environment["WHEEL_PURELIB"] = str(purelib)
    environment["WHEEL_INFRA_CACHE"] = str(tmp_path / "cache-infra")
    smoke = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path
        import shutil
        import sys

        from starlette.testclient import TestClient

        from src.gateway import cli
        from src.gateway import local_demo
        from src.gateway import local_server
        from src.gateway.deployment import agentcore_deploy

        installed = Path(os.environ["WHEEL_PURELIB"]).resolve()
        for module in (cli, local_demo, local_server, agentcore_deploy):
            assert Path(module.__file__).resolve().is_relative_to(installed)

        invocation_dir = Path.cwd()
        custom_config = invocation_dir / "custom" / "models.yaml"
        custom_config.parent.mkdir()
        shutil.copy2(
            local_server._RUNTIME_ROOT / "config" / "models.yaml",
            custom_config,
        )

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
        assert Path.cwd() == invocation_dir
        assert Path(config.models_config_path) == custom_config.resolve()
        assert Path(config.pricing_config_path).is_file()
        assert (local_server._RUNTIME_ROOT / "site" / "index.html").is_file()

        with TestClient(app, raise_server_exceptions=False) as client:
            redirect = client.get(
                "/admin/architecture",
                follow_redirects=False,
            )
            assert redirect.status_code == 307
            assert redirect.headers["location"] == "/architecture.html"
            architecture = client.get("/architecture.html")
            assert architecture.status_code == 200
            assert "How AxonLLM is built" in architecture.text

        agentcore_deploy.INFRA_ROOT = Path(
            os.environ["WHEEL_INFRA_CACHE"]
        )
        agentcore_deploy._materialize_infra()
        assert (agentcore_deploy.INFRA_ROOT / "app.py").is_file()
        assert (agentcore_deploy.INFRA_ROOT / "cdk.json").is_file()
        print(json.dumps({"routes": len(app.routes)}))
        """
    )
    completed = subprocess.run(
        [str(python), "-c", smoke],
        cwd=outside,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"routes":' in completed.stdout
