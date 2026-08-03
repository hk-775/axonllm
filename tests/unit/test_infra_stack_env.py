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
        assert isinstance(env["AXON_DYNAMODB_TABLE"], ast.Attribute)
        assert env["AXON_DYNAMODB_TABLE"].attr == "table_name"

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
