"""The image must contain the directories the request handlers read from.

Every other test in this suite runs against the repo working tree, where every
directory is present by definition. That makes an omitted ``COPY`` in the
Dockerfile invisible to all of them: the route tests pass, the handler works
locally, and the container 404s. That is exactly how ``site/`` came to be
missing from the image while ``TestLandingPage`` stayed green — the README's
first instruction ("docker compose up", then open localhost:8000) landed on a
stub, and nothing in CI could tell.

These tests read the Dockerfile as text rather than building an image, so they
need no Docker daemon and run in CI. That is a real limit: they check that a
path is declared, not that the built image serves it. The failure they exist to
catch is a forgotten line, which is the one that actually happened.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "Dockerfile"
_DOCKERIGNORE = _ROOT / ".dockerignore"


def _dockerfile_instructions() -> list[str]:
    """Return Dockerfile instructions with continued lines joined."""
    text = _DOCKERFILE.read_text(encoding="utf-8")
    text = re.sub(r"\\\s*\n", " ", text)
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _copied_paths() -> set[str]:
    """Sources named by a ``COPY`` in the Dockerfile, excluding ``--from`` ones.

    A ``COPY --from=...`` pulls out of another image rather than the build
    context, so it says nothing about what this repo ships.
    """
    paths: set[str] = set()
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("COPY") or "--from=" in line:
            continue
        # Last token is the destination; everything between is a source.
        for src in line.split()[1:-1]:
            paths.add(src.rstrip("/"))
    return paths


class TestTheImageShipsWhatTheHandlersRead:
    def test_the_landing_page_directory_is_copied(self):
        """``site/`` holds index.html, architecture.html and the demo video.

        The handler degrades to a 404 with a pointer to the dashboard rather
        than raising, which is correct for a pip install but is also why the
        omission produced no error anywhere — just a wrong page at the URL the
        README tells every new user to open first.
        """
        assert "site" in _copied_paths(), (
            "Dockerfile does not COPY site/ — the gateway root will 404 in the "
            "container while working locally"
        )

    def test_every_directory_a_route_reads_from_is_copied(self):
        """Derived from the handlers, so a new one cannot be forgotten here.

        ``_PROJECT_ROOT / "<name>"`` in the route module means a request reads
        that directory off disk at runtime; anything of that shape has to be in
        the image.
        """
        routes = (_ROOT / "src/gateway/admin/routes.py").read_text(encoding="utf-8")
        needed = set(re.findall(r'_PROJECT_ROOT\s*/\s*"([^"/]+)"', routes))
        # Only directories: a bare file at the root is copied by name.
        needed = {n for n in needed if (_ROOT / n).is_dir()}
        missing = needed - _copied_paths()
        assert not missing, f"read by a handler but never COPYed: {sorted(missing)}"


class TestTheCdkAppStaysOutOfTheImage:
    def test_site_infra_is_excluded(self):
        """``site/infra`` is the landing page's own CDK app — deploy-time only.

        Worth an explicit test because the exclusion is not obvious: the
        ``infra/`` line in .dockerignore is anchored to the build root, so it
        does **not** cover ``site/infra``. Verified by building, not assumed —
        a plain ``COPY site/ site/`` does ship it.
        """
        ignored = {
            ln.strip().rstrip("/")
            for ln in _DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        }
        assert "site/infra" in ignored, (
            "site/infra would be shipped: the root-anchored 'infra/' pattern "
            "does not match a nested path"
        )

    def test_the_http_handler_refuses_it_regardless(self):
        """Defence in depth: the exclusion above is not the only thing stopping it.

        If someone drops the .dockerignore line, the asset handler must still
        refuse the path — so this asserts the two independent reasons it does.
        """
        from src.gateway.admin.routes import (
            SITE_ASSET_DIRS,
            SITE_ASSET_TYPES,
            _is_servable_site_path,
        )

        assert ".py" not in SITE_ASSET_TYPES
        assert "infra" not in SITE_ASSET_DIRS
        assert not _is_servable_site_path(Path("infra/stack.py"))


class TestTheProductionRuntimeIsHardened:
    def test_the_final_user_is_a_dedicated_non_root_account(self):
        instructions = _dockerfile_instructions()
        users = [
            instruction.split(maxsplit=1)[1]
            for instruction in instructions
            if instruction.upper().startswith("USER ")
        ]
        assert users, "Dockerfile has no USER instruction, so the image runs as root"

        final_user = users[-1]
        match = re.fullmatch(r"(\d+):(\d+)", final_user)
        assert match, "final USER must use a stable numeric UID:GID"
        uid, gid = (int(part) for part in match.groups())
        assert uid > 0 and gid > 0, "final USER must not be root"

        build_steps = " ".join(
            instruction
            for instruction in instructions
            if instruction.upper().startswith("RUN ")
        )
        assert re.search(rf"\bgroupadd\b.*--gid\s+{gid}\b", build_steps)
        assert re.search(rf"\buseradd\b.*--uid\s+{uid}\b", build_steps)
        assert "chown -R root:root /app" in build_steps
        assert "chmod -R a-w /app" in build_steps

    def test_authentication_defaults_to_enforcement(self):
        environment = " ".join(
            instruction
            for instruction in _dockerfile_instructions()
            if instruction.upper().startswith("ENV ")
        )
        assert re.search(r"\bAXON_AUTH_MODE=ENFORCE\b", environment)
        assert re.search(r"\bAXON_DEPLOYMENT_PROFILE=production\b", environment)
        assert re.search(r"\bAXON_REQUIRE_CANONICAL_IDENTITY=true\b", environment)
        assert re.search(r"\bAXON_LOAD_DEMO_DATA=false\b", environment)
        assert re.search(r"\bAXON_NO_BROWSER=true\b", environment)

    def test_the_image_uses_the_fail_closed_standalone_host(self):
        instructions = _dockerfile_instructions()

        assert "STOPSIGNAL SIGTERM" in instructions
        assert 'CMD ["python", "-m", "src.gateway.standalone"]' in instructions
        healthchecks = [
            instruction
            for instruction in instructions
            if instruction.upper().startswith("HEALTHCHECK ")
        ]
        assert len(healthchecks) == 1
        assert "127.0.0.1:8000/health" in healthchecks[0]

    def test_every_sync_installs_the_locked_enterprise_extras(self):
        sync_steps = [
            instruction
            for instruction in _dockerfile_instructions()
            if instruction.upper().startswith("RUN ") and "uv sync" in instruction
        ]
        assert sync_steps, "Dockerfile does not install dependencies with uv sync"

        required_extras = {"server", "oidc", "otel"}
        for sync_step in sync_steps:
            assert "--frozen" in sync_step, "container dependencies must use uv.lock"
            installed_extras = set(
                re.findall(r"--extra(?:=|\s+)([a-z0-9-]+)", sync_step)
            )
            assert required_extras <= installed_extras
