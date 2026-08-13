"""The deployed container's environment, asserted against the CDK source.

`serve_dashboard.py` is the Dockerfile `CMD`, and it defaults
`AXON_LOAD_DEMO_DATA` to `true` when the variable is absent — a convenience for
a local run that becomes a liability on Fargate, where it seeds Acme Corp and 66
fabricated usage records into DynamoDB and they outlive the flag that created
them. The only place that default can be neutralised for every deploy is the
task definition's environment, so the value is asserted here.

Read as source rather than synthesized: `aws-cdk-lib` is not a test dependency
(`infra/requirements.txt` is installed separately, and CI installs only
`.[dev]`), and a `cdk synth` in the unit suite would trade a two-line parse for
a Node.js toolchain. What the parse cannot see is a value overridden later in
the stack or injected at deploy time — for that, `/admin/production-checklist`
checks the live environment, which is the assertion that actually matters in
production. This one catches the regression at the point it would be written.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_STACK = pathlib.Path(__file__).resolve().parents[2] / "infra" / "stack.py"


def _environment_dict() -> dict[str, ast.expr]:
    """The single `environment={...}` keyword in the CDK stack, as a dict.

    Fails loudly if there is more than one: a second container definition would
    silently make these assertions cover the wrong one.
    """
    tree = ast.parse(_STACK.read_text(encoding="utf-8"))
    found: list[ast.Dict] = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == "environment"
        and isinstance(node.value, ast.Dict)
    ]
    assert len(found) == 1, f"expected one environment={{...}} in {_STACK.name}, got {len(found)}"
    return {
        key.value: value
        for key, value in zip(found[0].keys, found[0].values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


class TestTheTaskDefinitionEnvironment:
    @pytest.fixture(scope="class")
    @classmethod
    def env(cls) -> dict[str, ast.expr]:
        return _environment_dict()

    def test_demo_data_is_explicitly_off(self, env):
        """The one value that has to be present rather than merely correct.

        An absent `AXON_LOAD_DEMO_DATA` is not a neutral default here — it is
        `true`, because the container `CMD` supplies one. This is the assertion
        that keeps a deploy from coming up with fictional tenants.
        """
        assert "AXON_LOAD_DEMO_DATA" in env, (
            "absent means demo data ON — serve_dashboard.py defaults it to 'true'"
        )
        node = env["AXON_LOAD_DEMO_DATA"]
        assert isinstance(node, ast.Constant), "must be a literal, not computed at synth time"
        assert node.value == "false"

    def test_auth_is_enforced(self, env):
        """Nothing behind a public ALB should accept unauthenticated requests.

        Same shape of bug as the line above: `serve_dashboard.py` defaults
        `AXON_AUTH_MODE` to `LOG_ONLY`, so dropping it here would open the
        deployment rather than fall back to the safe value.
        """
        node = env["AXON_AUTH_MODE"]
        assert isinstance(node, ast.Constant) and node.value == "ENFORCE"

    def test_persistence_is_on_and_names_the_table_construct(self, env):
        """A deploy without persistence loses every project on task restart.

        The table name is deliberately *not* asserted as a literal: it comes
        from the construct (`state_table.table_name`), which is what keeps the
        env var and the DynamoDB table from drifting apart.
        """
        assert isinstance(env["LLM_ROUTER_DYNAMODB_ENABLED"], ast.Constant)
        assert env["LLM_ROUTER_DYNAMODB_ENABLED"].value == "true"
        assert isinstance(env["AXON_DYNAMODB_TABLE"], ast.Name)
        assert env["AXON_DYNAMODB_TABLE"].id == (
            "selected_state_table_name"
        )

    def test_routing_configuration_is_kms_signed(self, env):
        mode = env["AXON_ROUTING_CONFIG_SIGNING_MODE"]
        assert isinstance(mode, ast.Constant)
        assert mode.value == "sign-verify"
        key = env["AXON_ROUTING_CONFIG_SIGNING_KEY_ARN"]
        assert isinstance(key, ast.Attribute)
        assert key.attr == "key_arn"

    def test_the_port_matches_the_container_port(self, env):
        """`AXON_SERVER_PORT` is what uvicorn binds; `container_port` is what the
        target group health-checks. A mismatch fails the health check with a
        perfectly healthy process behind it."""
        assert env["AXON_SERVER_PORT"].value == "8000"

        tree = ast.parse(_STACK.read_text(encoding="utf-8"))
        ports = [
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg == "container_port"
            and isinstance(node.value, ast.Constant)
        ]
        assert ports == [8000]


def _keyword_literals(name: str) -> list[str]:
    """Every `name="literal"` keyword argument in the CDK stack."""
    tree = ast.parse(_STACK.read_text(encoding="utf-8"))
    return [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == name
        and isinstance(node.value, ast.Constant)
    ]


class TestTheResourcesTheDocsAddressByName:
    """Physical names for resources the README still tells operators to type.

    Cluster, service, and task-definition instructions use literal names. The
    retained provider secret deliberately does not: operators resolve its
    generated name through the ``ProviderSecretArn`` stack output so a retained
    secret cannot block replacement-stack creation.

    Asserted here rather than left to review because the failure is invisible from
    both sides — the stack deploys perfectly, the docs read perfectly, and they
    only disagree once someone runs step 3 on a live environment. If a name here
    changes, the README changes with it.
    """

    @pytest.mark.parametrize(
        ("keyword", "doc_flag"),
        [
            ("cluster_name", "--cluster axonllm"),
            ("service_name", "--service axonllm"),
            ("family", "--task-definition axonllm"),
        ],
    )
    def test_the_name_the_readme_uses_is_pinned(self, keyword, doc_flag):
        names = _keyword_literals(keyword)
        assert names == ["axonllm"], (
            f"{keyword} must be 'axonllm' — the README documents `{doc_flag}`, and "
            f"an unset {keyword} makes CDK generate a name that command cannot resolve"
        )

    def test_the_retained_secret_uses_a_generated_name(self):
        """A fixed retained name would make a replacement stack undeployable."""
        assert _keyword_literals("secret_name") == []
        source = _STACK.read_text(encoding="utf-8")
        assert '"ProviderSecretArn"' in source
        assert "value=api_keys_secret.secret_arn" in source

    def test_scim_credentials_use_an_optional_exact_secret_arn(self):
        source = _STACK.read_text(encoding="utf-8")

        assert 'try_get_context("scim_tenants_secret_arn")' in source
        assert "Secret.from_secret_complete_arn" in source
        assert 'container_secrets["AXON_SCIM_TENANTS"]' in source
        assert '"ScimTenantsSecretArn"' in source

    def test_the_table_name_defaults_to_the_documented_one_but_is_overridable(self):
        """`axonllm-state` stays the default, and `-c table_name=` can replace it.

        Both halves matter. The default is what `axon issue-key` is documented
        against, so it cannot drift. The override exists because RETAIN plus a
        fixed name makes the stack un-redeployable after a destroy: the table
        survives unowned, and the next deploy fails on "already exists" before
        creating anything. Asserted as an override rather than a literal so a
        future edit cannot quietly hardcode it back.
        """
        source = _STACK.read_text(encoding="utf-8")
        assert 'try_get_context("table_name")' in source, (
            "table_name must be overridable — a hardcoded RETAIN table cannot be redeployed"
        )
        assert '"axonllm-state"' in source, "the documented default must survive"


class TestTheCdkAppFindsItsDependencies:
    """`cdk.json` must name the venv interpreter, not a bare `python3`.

    `aws-cdk-lib` lives in `infra/.venv` — deliberately not a root dependency,
    since nothing at runtime imports it. So `"app": "python3 app.py"` resolved to
    whatever `python3` was on PATH, which does not see that venv, and every CDK
    command died with `ModuleNotFoundError: No module named 'aws_cdk'` *after*
    printing enough banner output to bury the cause.

    This is the third instance of one pattern in this repo: `deploy-fargate.sh`
    activated the venv first and worked, so the script was fine while the
    documented command was broken. Asserted here because the unit suite cannot
    run `cdk` itself (no Node toolchain, and `aws-cdk-lib` is not a test
    dependency), which is precisely why the bug reached a release.
    """

    _CDK_JSON = _STACK.parent / "cdk.json"

    def test_the_app_command_uses_the_venv_interpreter(self):
        import json

        app = json.loads(self._CDK_JSON.read_text(encoding="utf-8"))["app"]
        assert app.startswith(".venv/bin/python"), (
            f'cdk.json "app" is {app!r}; a bare python3 cannot import aws_cdk, '
            "which is installed in infra/.venv"
        )

    def test_the_interpreter_path_is_relative_to_cdk_json(self):
        """The CDK CLI runs `app` with cdk.json's directory as cwd.

        An absolute path would be machine-specific and a `infra/`-prefixed one
        would resolve to `infra/infra/.venv` — both break for everyone but the
        author, and neither shows up until someone else clones the repo.
        """
        import json

        app = json.loads(self._CDK_JSON.read_text(encoding="utf-8"))["app"]
        assert not app.startswith("/"), "must be relative — an absolute path is machine-specific"
        assert "infra/.venv" not in app, "cwd is already infra/; this would be infra/infra/.venv"
