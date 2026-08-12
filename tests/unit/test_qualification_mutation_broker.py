from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from src.gateway.deployment import qualification_mutation_broker as broker


ACCOUNT = "123456789012"
REGION = "us-east-1"
STACK_ID = "11111111-2222-3333-4444-555555555555"
AGENTCORE_STACK_ARN = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{broker.MANAGED_AGENTCORE_STACK_NAME}/{STACK_ID}"
)
CONTROL_PLANE_STACK_ARN = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{broker.MANAGED_CONTROL_PLANE_STACK_NAME}/{STACK_ID}"
)
EXECUTION_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/cdk-axqual-cfn-exec-role-{ACCOUNT}-{REGION}"
AUTHORIZATION_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/axonllm-qualification-mutation-authorizations"
PRIMARY_TABLE = broker.MANAGED_PRIMARY_TABLE_NAME
RESTORED_PREFIX = f"{PRIMARY_TABLE}-restore-validation-"
RESTORED_TABLE = f"{RESTORED_PREFIX}20260812-abcd"
AUTHORIZATION_ID = "authorization-001"
OWNER_ID = "owner-001"
FENCE_TOKEN = 17
APPROVAL_ID = "CHG-2026-001"
NOW = 1_786_536_000
_UNSET = object()


def _environment() -> dict[str, str]:
    return {
        broker.AUTHORIZATION_TABLE_ENV: AUTHORIZATION_TABLE_ARN,
        broker.PRIMARY_TABLE_NAME_ENV: PRIMARY_TABLE,
        broker.EXECUTION_ROLE_ARN_ENV: EXECUTION_ROLE_ARN,
    }


def _event(
    *,
    stack_kind: str = "agentcore",
    legal_edge: str = "quiesce-primary",
    authorization_id: str = AUTHORIZATION_ID,
    owner_id: str = OWNER_ID,
    fence_token: int = FENCE_TOKEN,
) -> dict[str, Any]:
    return {
        "schema": broker.EVENT_SCHEMA,
        "version": broker.EVENT_VERSION,
        "authorizationId": authorization_id,
        "ownerId": owner_id,
        "fenceToken": fence_token,
        "stackKind": stack_kind,
        "legalEdge": legal_edge,
    }


def _wire(value: str | int) -> dict[str, str]:
    if isinstance(value, int):
        return {"N": str(value)}
    return {"S": value}


def _authorization_item(
    *,
    stack_kind: str = "agentcore",
    legal_edge: str = "quiesce-primary",
    authorization_id: str = AUTHORIZATION_ID,
    owner_id: str = OWNER_ID,
    fence_token: int = FENCE_TOKEN,
    status: str = "ACTIVE",
    expires_at_epoch: int = NOW + 600,
    stack_arn: str | None = None,
    primary_table_name: str = PRIMARY_TABLE,
    restored_table_name: str = RESTORED_TABLE,
    approval_id: str = APPROVAL_ID,
    execution_role_arn: str = EXECUTION_ROLE_ARN,
) -> dict[str, dict[str, str]]:
    if stack_arn is None:
        stack_arn = AGENTCORE_STACK_ARN if stack_kind == "agentcore" else CONTROL_PLANE_STACK_ARN
    values: dict[str, str | int] = {
        "schema": broker.AUTHORIZATION_SCHEMA,
        "version": broker.AUTHORIZATION_VERSION,
        "authorizationId": authorization_id,
        "ownerId": owner_id,
        "fenceToken": fence_token,
        "stackKind": stack_kind,
        "legalEdge": legal_edge,
        "status": status,
        "expiresAtEpoch": expires_at_epoch,
        "stackArn": stack_arn,
        "primaryTableName": primary_table_name,
        "restoredTableName": restored_table_name,
        "approvalId": approval_id,
        "executionRoleArn": execution_role_arn,
    }
    return {name: _wire(value) for name, value in values.items()}


def _outputs(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"OutputKey": name, "OutputValue": value} for name, value in values.items()]


def _stack(
    *,
    stack_kind: str = "agentcore",
    stack_arn: str | None = None,
    mode: str = "normal",
    selected_table: str = PRIMARY_TABLE,
    approval_id: str = "CHG-2026-000",
    status: str = "UPDATE_COMPLETE",
) -> dict[str, Any]:
    if stack_kind == "agentcore":
        stack_arn = AGENTCORE_STACK_ARN if stack_arn is None else stack_arn
        stack_name = broker.MANAGED_AGENTCORE_STACK_NAME
        values = {
            "StateTableName": PRIMARY_TABLE,
            "SelectedRuntimeStateTableName": selected_table,
            "RecoveryCutoverMode": mode,
            "RecoveryApprovalId": approval_id,
        }
    else:
        stack_arn = CONTROL_PLANE_STACK_ARN if stack_arn is None else stack_arn
        stack_name = broker.MANAGED_CONTROL_PLANE_STACK_NAME
        values = {
            "AgentCoreStackName": broker.MANAGED_AGENTCORE_STACK_NAME,
            "PrimaryStateTableName": PRIMARY_TABLE,
            "SelectedRuntimeStateTableName": selected_table,
            "RecoveryCutoverMode": mode,
            "RecoveryApprovalId": approval_id,
        }
    return {
        "StackId": stack_arn,
        "StackName": stack_name,
        "StackStatus": status,
        "RoleARN": EXECUTION_ROLE_ARN,
        "Parameters": [
            {"ParameterKey": "DangerousUnchangedInput", "ParameterValue": "do-not-copy"},
            {"ParameterKey": "RecoveryApprovalId", "ParameterValue": approval_id},
            {"ParameterKey": "RecoveryCutoverMode", "ParameterValue": mode},
            {"ParameterKey": "RuntimeStateTableName", "ParameterValue": selected_table},
        ],
        "Outputs": _outputs(values),
    }


class FakeDynamoDB:
    def __init__(
        self,
        item: dict[str, dict[str, str]] | None,
        *,
        response: Any = _UNSET,
        error: Exception | None = None,
    ) -> None:
        self.item = deepcopy(item)
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> Any:
        self.calls.append(deepcopy(kwargs))
        if self.error is not None:
            raise self.error
        if self.response is not _UNSET:
            return deepcopy(self.response)
        return {} if self.item is None else {"Item": deepcopy(self.item)}


class FakeCloudFormation:
    def __init__(
        self,
        stack: dict[str, Any],
        *,
        describe_response: Any = _UNSET,
        describe_error: Exception | None = None,
        update_response: Any = _UNSET,
        update_error: Exception | None = None,
    ) -> None:
        self.stack = deepcopy(stack)
        self.describe_response = describe_response
        self.describe_error = describe_error
        self.update_response = update_response
        self.update_error = update_error
        self.describe_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def describe_stacks(self, **kwargs: Any) -> Any:
        self.describe_calls.append(deepcopy(kwargs))
        if len(self.describe_calls) > 1:
            raise AssertionError("broker described more than once")
        if self.describe_error is not None:
            raise self.describe_error
        if self.describe_response is not _UNSET:
            return deepcopy(self.describe_response)
        return {"Stacks": [deepcopy(self.stack)]}

    def update_stack(self, **kwargs: Any) -> Any:
        self.update_calls.append(deepcopy(kwargs))
        if len(self.update_calls) > 1:
            raise AssertionError("broker updated more than once")
        if self.update_error is not None:
            raise self.update_error
        if self.update_response is not _UNSET:
            return deepcopy(self.update_response)
        return {"StackId": self.stack["StackId"]}


def _invoke(
    *,
    stack_kind: str = "agentcore",
    legal_edge: str = "quiesce-primary",
    mode: str = "normal",
    selected_table: str = PRIMARY_TABLE,
    stack_approval_id: str = "CHG-2026-000",
    status: str = "UPDATE_COMPLETE",
    item: dict[str, dict[str, str]] | None = None,
    event: Any | None = None,
    environ: dict[str, str] | None = None,
    dynamodb: FakeDynamoDB | None = None,
    cloudformation: FakeCloudFormation | None = None,
) -> tuple[dict[str, str], FakeDynamoDB, FakeCloudFormation]:
    if item is None:
        item = _authorization_item(
            stack_kind=stack_kind,
            legal_edge=legal_edge,
        )
    if event is None:
        event = _event(
            stack_kind=stack_kind,
            legal_edge=legal_edge,
        )
    if dynamodb is None:
        dynamodb = FakeDynamoDB(item)
    if cloudformation is None:
        cloudformation = FakeCloudFormation(
            _stack(
                stack_kind=stack_kind,
                mode=mode,
                selected_table=selected_table,
                approval_id=stack_approval_id,
                status=status,
            )
        )
    result = broker.handle_event(
        event,
        dynamodb_client=dynamodb,
        cloudformation_client=cloudformation,
        environ=_environment() if environ is None else environ,
        clock=lambda: NOW,
    )
    return result, dynamodb, cloudformation


def _parameter_map(call: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {parameter["ParameterKey"]: parameter for parameter in call["Parameters"]}


@pytest.mark.parametrize(
    (
        "stack_kind",
        "legal_edge",
        "mode",
        "selected_table",
        "stack_approval",
        "next_mode",
        "target_parameter",
    ),
    [
        (
            "agentcore",
            "quiesce-primary",
            "normal",
            PRIMARY_TABLE,
            "CHG-2026-000",
            "quiesced",
            "",
        ),
        (
            "control-plane",
            "quiesce-restored",
            "normal",
            RESTORED_TABLE,
            APPROVAL_ID,
            "quiesced",
            RESTORED_TABLE,
        ),
        (
            "agentcore",
            "cutover-to-restored",
            "quiesced",
            PRIMARY_TABLE,
            APPROVAL_ID,
            "selected",
            RESTORED_TABLE,
        ),
        (
            "control-plane",
            "cutover-to-primary",
            "quiesced",
            RESTORED_TABLE,
            APPROVAL_ID,
            "selected",
            "",
        ),
        (
            "control-plane",
            "resume-restored",
            "selected",
            RESTORED_TABLE,
            APPROVAL_ID,
            "normal",
            RESTORED_TABLE,
        ),
        (
            "control-plane",
            "resume-primary",
            "selected",
            PRIMARY_TABLE,
            APPROVAL_ID,
            "normal",
            "",
        ),
    ],
)
def test_derives_only_the_legal_update(
    stack_kind: str,
    legal_edge: str,
    mode: str,
    selected_table: str,
    stack_approval: str,
    next_mode: str,
    target_parameter: str,
) -> None:
    result, dynamodb, cloudformation = _invoke(
        stack_kind=stack_kind,
        legal_edge=legal_edge,
        mode=mode,
        selected_table=selected_table,
        stack_approval_id=stack_approval,
    )

    assert result == {"status": "PENDING"}
    assert dynamodb.calls == [
        {
            "TableName": AUTHORIZATION_TABLE_ARN,
            "Key": {"authorizationId": {"S": AUTHORIZATION_ID}},
            "ConsistentRead": True,
        }
    ]
    expected_stack = AGENTCORE_STACK_ARN if stack_kind == "agentcore" else CONTROL_PLANE_STACK_ARN
    assert cloudformation.describe_calls == [{"StackName": expected_stack}]
    assert len(cloudformation.update_calls) == 1
    update = cloudformation.update_calls[0]
    assert set(update) == {
        "StackName",
        "UsePreviousTemplate",
        "RoleARN",
        "Capabilities",
        "Parameters",
        "ClientRequestToken",
    }
    assert update["StackName"] == expected_stack
    assert update["UsePreviousTemplate"] is True
    assert update["RoleARN"] == EXECUTION_ROLE_ARN
    assert update["Capabilities"] == ["CAPABILITY_NAMED_IAM"]
    assert update["ClientRequestToken"].startswith("axonllm-qsm-")
    assert len(update["ClientRequestToken"]) == len("axonllm-qsm-") + 64
    parameters = _parameter_map(update)
    assert parameters == {
        "DangerousUnchangedInput": {
            "ParameterKey": "DangerousUnchangedInput",
            "UsePreviousValue": True,
        },
        "RecoveryApprovalId": {
            "ParameterKey": "RecoveryApprovalId",
            "ParameterValue": APPROVAL_ID,
        },
        "RecoveryCutoverMode": {
            "ParameterKey": "RecoveryCutoverMode",
            "ParameterValue": next_mode,
        },
        "RuntimeStateTableName": {
            "ParameterKey": "RuntimeStateTableName",
            "ParameterValue": target_parameter,
        },
    }
    assert "do-not-copy" not in repr(update)
    assert not {
        "TemplateBody",
        "TemplateURL",
        "StackPolicyBody",
        "StackPolicyURL",
        "Tags",
    }.intersection(update)


def test_agentcore_cutover_stops_at_validation_until_separate_resume() -> None:
    selected_result, _, selected_cloudformation = _invoke(
        legal_edge="cutover-to-restored",
        mode="selected",
        selected_table=RESTORED_TABLE,
        stack_approval_id=APPROVAL_ID,
    )
    cutover_complete, _, cutover_complete_cloudformation = _invoke(
        legal_edge="cutover-to-restored",
        mode="validation",
        selected_table=RESTORED_TABLE,
        stack_approval_id=APPROVAL_ID,
    )
    validation_result, _, validation_cloudformation = _invoke(
        legal_edge="resume-restored",
        mode="validation",
        selected_table=RESTORED_TABLE,
        stack_approval_id=APPROVAL_ID,
    )
    complete_result, _, complete_cloudformation = _invoke(
        legal_edge="resume-restored",
        mode="normal",
        selected_table=RESTORED_TABLE,
        stack_approval_id=APPROVAL_ID,
    )

    assert selected_result == {"status": "PENDING"}
    assert cutover_complete == {"status": "COMPLETE"}
    assert validation_result == {"status": "PENDING"}
    assert complete_result == {"status": "COMPLETE"}
    selected_update = selected_cloudformation.update_calls[0]
    validation_update = validation_cloudformation.update_calls[0]
    assert _parameter_map(selected_update)["RecoveryCutoverMode"]["ParameterValue"] == "validation"
    assert _parameter_map(validation_update)["RecoveryCutoverMode"]["ParameterValue"] == "normal"
    assert selected_update["ClientRequestToken"] != validation_update["ClientRequestToken"]
    assert cutover_complete_cloudformation.update_calls == []
    assert complete_cloudformation.update_calls == []


@pytest.mark.parametrize(
    ("stack_kind", "legal_edge", "mode", "selected_table"),
    [
        ("agentcore", "quiesce-primary", "quiesced", PRIMARY_TABLE),
        ("control-plane", "quiesce-restored", "quiesced", RESTORED_TABLE),
        ("agentcore", "cutover-to-restored", "validation", RESTORED_TABLE),
        ("control-plane", "cutover-to-primary", "selected", PRIMARY_TABLE),
        ("agentcore", "resume-restored", "normal", RESTORED_TABLE),
        ("control-plane", "resume-primary", "normal", PRIMARY_TABLE),
    ],
)
def test_completed_edge_does_not_update(
    stack_kind: str,
    legal_edge: str,
    mode: str,
    selected_table: str,
) -> None:
    result, _, cloudformation = _invoke(
        stack_kind=stack_kind,
        legal_edge=legal_edge,
        mode=mode,
        selected_table=selected_table,
        stack_approval_id=APPROVAL_ID,
    )

    assert result == {"status": "COMPLETE"}
    assert len(cloudformation.describe_calls) == 1
    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    "status",
    [
        "UPDATE_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    ],
)
def test_update_in_progress_returns_pending_without_another_update(
    status: str,
) -> None:
    result, _, cloudformation = _invoke(status=status)

    assert result == {"status": "PENDING"}
    assert len(cloudformation.describe_calls) == 1
    assert cloudformation.update_calls == []


def test_client_request_token_is_deterministic_and_authorization_bound() -> None:
    first, _, first_cloudformation = _invoke()
    second, _, second_cloudformation = _invoke()
    changed_item = _authorization_item(
        authorization_id="authorization-002",
    )
    changed_event = _event(authorization_id="authorization-002")
    changed, _, changed_cloudformation = _invoke(
        item=changed_item,
        event=changed_event,
    )

    assert first == second == changed == {"status": "PENDING"}
    first_token = first_cloudformation.update_calls[0]["ClientRequestToken"]
    second_token = second_cloudformation.update_calls[0]["ClientRequestToken"]
    changed_token = changed_cloudformation.update_calls[0]["ClientRequestToken"]
    assert first_token == second_token
    assert changed_token != first_token


@pytest.mark.parametrize(
    ("stack_kind", "stack_name", "stack_id", "restored_table"),
    [
        (
            "agentcore",
            broker.MANAGED_AGENTCORE_STACK_NAME,
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            f"{RESTORED_PREFIX}20260813-ef01",
        ),
        (
            "control-plane",
            broker.MANAGED_CONTROL_PLANE_STACK_NAME,
            "ffffffff-1111-2222-3333-444444444444",
            f"{RESTORED_PREFIX}{'x' * (255 - len(RESTORED_PREFIX))}",
        ),
    ],
)
def test_authorization_record_supplies_dynamic_stack_and_restored_targets(
    stack_kind: str,
    stack_name: str,
    stack_id: str,
    restored_table: str,
) -> None:
    stack_arn = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{stack_name}/{stack_id}"
    item = _authorization_item(
        stack_kind=stack_kind,
        legal_edge="cutover-to-restored",
        stack_arn=stack_arn,
        restored_table_name=restored_table,
    )
    cloudformation = FakeCloudFormation(
        _stack(
            stack_kind=stack_kind,
            stack_arn=stack_arn,
            mode="quiesced",
            selected_table=PRIMARY_TABLE,
            approval_id=APPROVAL_ID,
        )
    )

    result, _, cloudformation = _invoke(
        stack_kind=stack_kind,
        legal_edge="cutover-to-restored",
        item=item,
        cloudformation=cloudformation,
    )

    assert result == {"status": "PENDING"}
    assert cloudformation.describe_calls == [{"StackName": stack_arn}]
    update = cloudformation.update_calls[0]
    assert update["StackName"] == stack_arn
    assert _parameter_map(update)["RuntimeStateTableName"]["ParameterValue"] == restored_table


def _assert_rejected_before_authorization(event: Any) -> None:
    dynamodb = FakeDynamoDB(_authorization_item())
    cloudformation = FakeCloudFormation(_stack())
    with pytest.raises(broker.QualificationMutationError):
        broker.handle_event(
            event,
            dynamodb_client=dynamodb,
            cloudformation_client=cloudformation,
            environ=_environment(),
            clock=lambda: NOW,
        )
    assert dynamodb.calls == []
    assert cloudformation.describe_calls == []
    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        "{}",
        {name: value for name, value in _event().items() if name != "ownerId"},
        {**_event(), "parameters": {}},
        {**_event(), "RoleARN": EXECUTION_ROLE_ARN},
        {**_event(), "TemplateURL": "https://attacker.invalid/template"},
        {**_event(), "schema": "axonllm.qualification-selector-mutation/v2"},
        {**_event(), "version": 2},
        {**_event(), "version": True},
        {**_event(), "version": 1.0},
        {**_event(), "version": "1"},
        {**_event(), "authorizationId": ""},
        {**_event(), "authorizationId": "bad value"},
        {**_event(), "authorizationId": "é"},
        {**_event(), "ownerId": ""},
        {**_event(), "ownerId": "x" * 129},
        {**_event(), "fenceToken": True},
        {**_event(), "fenceToken": 0},
        {**_event(), "fenceToken": -1},
        {**_event(), "fenceToken": str(FENCE_TOKEN)},
        {**_event(), "fenceToken": 2**63},
        {**_event(), "stackKind": "production"},
        {**_event(), "stackKind": ["agentcore"]},
        {**_event(), "legalEdge": "normal-to-anything"},
        {**_event(), "legalEdge": {"edge": "quiesce-primary"}},
    ],
)
def test_event_schema_rejects_missing_extra_and_malformed_values(
    event: Any,
) -> None:
    _assert_rejected_before_authorization(event)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "axonllm.qualification-selector-authorization/v2"),
        ("version", 2),
        ("authorizationId", "authorization-foreign"),
        ("ownerId", "owner-foreign"),
        ("fenceToken", FENCE_TOKEN + 1),
        ("stackKind", "control-plane"),
        ("legalEdge", "resume-primary"),
        ("status", "REVOKED"),
        ("expiresAtEpoch", NOW),
        ("expiresAtEpoch", NOW - 1),
        (
            "stackArn",
            AGENTCORE_STACK_ARN.replace(ACCOUNT, "999999999999"),
        ),
        ("primaryTableName", "axonllm-agentcore-state"),
        (
            "restoredTableName",
            f"{PRIMARY_TABLE}-restore-copy-foreign",
        ),
        ("approvalId", "x"),
        (
            "executionRoleArn",
            f"arn:aws:iam::{ACCOUNT}:role/attacker",
        ),
    ],
)
def test_authorization_record_must_match_every_binding(
    field: str,
    value: str | int,
) -> None:
    item = _authorization_item()
    item[field] = _wire(value)
    dynamodb = FakeDynamoDB(item)
    cloudformation = FakeCloudFormation(_stack())

    with pytest.raises(broker.QualificationMutationError):
        _invoke(dynamodb=dynamodb, cloudformation=cloudformation)

    assert len(dynamodb.calls) == 1
    assert cloudformation.describe_calls == []
    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    "stack_arn",
    [
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/AxonLLMAgentCoreStack/{STACK_ID}",
        CONTROL_PLANE_STACK_ARN,
        AGENTCORE_STACK_ARN.replace(REGION, "us-west-2"),
        AGENTCORE_STACK_ARN.replace(ACCOUNT, "999999999999"),
        AGENTCORE_STACK_ARN.replace("arn:aws:", "arn:aws-cn:"),
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{broker.MANAGED_AGENTCORE_STACK_NAME}/short",
        (f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{broker.MANAGED_AGENTCORE_STACK_NAME}-other/{STACK_ID}"),
    ],
)
def test_authorized_stack_requires_exact_managed_name_and_trusted_identity(
    stack_arn: str,
) -> None:
    dynamodb = FakeDynamoDB(_authorization_item(stack_arn=stack_arn))
    cloudformation = FakeCloudFormation(_stack())

    with pytest.raises(broker.QualificationMutationError):
        _invoke(dynamodb=dynamodb, cloudformation=cloudformation)

    assert len(dynamodb.calls) == 1
    assert cloudformation.describe_calls == []
    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    "restored_table",
    [
        PRIMARY_TABLE,
        RESTORED_PREFIX,
        f"{PRIMARY_TABLE}-restore-validation",
        f"{PRIMARY_TABLE}-restore-copy-20260812",
        f"foreign-{RESTORED_TABLE}",
        f"{RESTORED_PREFIX}{'x' * (256 - len(RESTORED_PREFIX))}",
        f"{RESTORED_PREFIX}bad/name",
        f"{RESTORED_PREFIX}bad name",
    ],
)
def test_authorized_restored_table_is_bounded_to_the_primary_restore_namespace(
    restored_table: str,
) -> None:
    dynamodb = FakeDynamoDB(_authorization_item(restored_table_name=restored_table))
    cloudformation = FakeCloudFormation(_stack())

    with pytest.raises(broker.QualificationMutationError):
        _invoke(dynamodb=dynamodb, cloudformation=cloudformation)

    assert len(dynamodb.calls) == 1
    assert cloudformation.describe_calls == []
    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.pop("approvalId"),
        lambda item: item.update({"unexpected": {"S": "smuggled"}}),
        lambda item: item.update({"fenceToken": {"S": str(FENCE_TOKEN)}}),
        lambda item: item.update({"fenceToken": {"N": "017"}}),
        lambda item: item.update({"fenceToken": {"N": "17.0"}}),
        lambda item: item.update({"expiresAtEpoch": {"N": "-1"}}),
        lambda item: item.update({"version": {"N": str(2**63)}}),
        lambda item: item.update({"approvalId": {"S": APPROVAL_ID, "N": "1"}}),
    ],
)
def test_authorization_record_rejects_noncanonical_dynamodb_values(
    mutate: Any,
) -> None:
    item = _authorization_item()
    mutate(item)
    dynamodb = FakeDynamoDB(item)
    cloudformation = FakeCloudFormation(_stack())

    with pytest.raises(broker.QualificationMutationError):
        _invoke(dynamodb=dynamodb, cloudformation=cloudformation)

    assert cloudformation.describe_calls == []
    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"Item": []},
        {"Item": "record"},
    ],
)
def test_missing_or_malformed_authorization_response_fails_closed(
    response: Any,
) -> None:
    dynamodb = FakeDynamoDB(None, response=response)
    cloudformation = FakeCloudFormation(_stack())

    with pytest.raises(broker.QualificationMutationError):
        _invoke(dynamodb=dynamodb, cloudformation=cloudformation)

    assert cloudformation.describe_calls == []
    assert cloudformation.update_calls == []


def test_authorization_read_failure_is_wrapped_and_stops() -> None:
    dynamodb = FakeDynamoDB(None, error=TimeoutError("network"))
    cloudformation = FakeCloudFormation(_stack())

    with pytest.raises(
        broker.QualificationMutationError,
        match="could not be read",
    ):
        _invoke(dynamodb=dynamodb, cloudformation=cloudformation)

    assert cloudformation.describe_calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda env: env.pop(broker.AUTHORIZATION_TABLE_ENV),
        lambda env: env.pop(broker.PRIMARY_TABLE_NAME_ENV),
        lambda env: env.pop(broker.EXECUTION_ROLE_ARN_ENV),
        lambda env: env.update({broker.AUTHORIZATION_TABLE_ENV: "axonllm-authorizations"}),
        lambda env: env.update({broker.AUTHORIZATION_TABLE_ENV: "bad/table"}),
        lambda env: env.update({broker.AUTHORIZATION_TABLE_ENV: AUTHORIZATION_TABLE_ARN.replace(REGION, "us-west-2")}),
        lambda env: env.update(
            {
                broker.AUTHORIZATION_TABLE_ENV: AUTHORIZATION_TABLE_ARN.replace(
                    ACCOUNT,
                    "999999999999",
                )
            }
        ),
        lambda env: env.update(
            {
                broker.AUTHORIZATION_TABLE_ENV: AUTHORIZATION_TABLE_ARN.replace(
                    "arn:aws:",
                    "arn:aws-cn:",
                )
            }
        ),
        lambda env: env.update({broker.PRIMARY_TABLE_NAME_ENV: ("axonllm-agentcore-state-production")}),
        lambda env: env.update({broker.EXECUTION_ROLE_ARN_ENV: (f"arn:aws:iam::{ACCOUNT}:role/cfn-admin")}),
        lambda env: env.update(
            {
                broker.EXECUTION_ROLE_ARN_ENV: EXECUTION_ROLE_ARN.replace(
                    "cdk-axqual-",
                    "cdk-axprod-",
                )
            }
        ),
        lambda env: env.update(
            {
                broker.EXECUTION_ROLE_ARN_ENV: EXECUTION_ROLE_ARN.replace(
                    REGION,
                    "us-west-2",
                )
            }
        ),
        lambda env: env.update(
            {
                broker.EXECUTION_ROLE_ARN_ENV: EXECUTION_ROLE_ARN.replace(
                    ACCOUNT,
                    "999999999999",
                    1,
                )
            }
        ),
    ],
)
def test_environment_is_an_exact_qualification_trust_anchor(
    mutation: Any,
) -> None:
    environ = _environment()
    mutation(environ)
    dynamodb = FakeDynamoDB(_authorization_item())
    cloudformation = FakeCloudFormation(_stack())

    with pytest.raises(broker.QualificationMutationError):
        _invoke(
            environ=environ,
            dynamodb=dynamodb,
            cloudformation=cloudformation,
        )

    assert dynamodb.calls == []
    assert cloudformation.describe_calls == []
    assert cloudformation.update_calls == []


def test_legacy_dynamic_environment_values_cannot_override_authorization() -> None:
    environ = {
        **_environment(),
        "AXON_QUALIFICATION_AGENTCORE_STACK_ARN": CONTROL_PLANE_STACK_ARN,
        "AXON_QUALIFICATION_CONTROL_PLANE_STACK_ARN": AGENTCORE_STACK_ARN,
        "AXON_QUALIFICATION_RESTORED_TABLE_NAME": f"{RESTORED_PREFIX}attacker",
    }

    result, _, cloudformation = _invoke(environ=environ)

    assert result == {"status": "PENDING"}
    assert cloudformation.describe_calls == [{"StackName": AGENTCORE_STACK_ARN}]
    assert cloudformation.update_calls[0]["StackName"] == AGENTCORE_STACK_ARN


def _set_output(stack: dict[str, Any], name: str, value: str) -> None:
    for output in stack["Outputs"]:
        if output["OutputKey"] == name:
            output["OutputValue"] = value
            return
    raise AssertionError(name)


def _drop_output(stack: dict[str, Any], name: str) -> None:
    stack["Outputs"] = [output for output in stack["Outputs"] if output["OutputKey"] != name]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda stack: stack.update({"StackId": stack["StackId"].replace(ACCOUNT, "999999999999")}),
        lambda stack: stack.update({"StackName": "AxonLLMAgentCoreStack"}),
        lambda stack: stack.update({"RoleARN": f"arn:aws:iam::{ACCOUNT}:role/attacker"}),
        lambda stack: stack.update({"StackStatus": "UPDATE_ROLLBACK_COMPLETE"}),
        lambda stack: stack.update({"Outputs": "not-a-list"}),
        lambda stack: _drop_output(stack, "RecoveryCutoverMode"),
        lambda stack: _set_output(
            stack,
            "StateTableName",
            "axonllm-agentcore-state",
        ),
        lambda stack: _set_output(
            stack,
            "SelectedRuntimeStateTableName",
            "foreign-table",
        ),
        lambda stack: _set_output(
            stack,
            "RecoveryCutoverMode",
            "arbitrary",
        ),
        lambda stack: _set_output(
            stack,
            "RecoveryApprovalId",
            "bad approval",
        ),
        lambda stack: stack["Outputs"].append(
            {
                "OutputKey": "RecoveryCutoverMode",
                "OutputValue": "normal",
            }
        ),
        lambda stack: stack.update({"Parameters": "not-a-list"}),
        lambda stack: stack.update(
            {
                "Parameters": [
                    parameter
                    for parameter in stack["Parameters"]
                    if parameter["ParameterKey"] != "RuntimeStateTableName"
                ]
            }
        ),
        lambda stack: stack["Parameters"].append(
            {
                "ParameterKey": "RecoveryCutoverMode",
                "ParameterValue": "normal",
            }
        ),
    ],
)
def test_malformed_or_foreign_stack_state_never_reaches_update(
    mutation: Any,
) -> None:
    stack = _stack()
    mutation(stack)
    cloudformation = FakeCloudFormation(stack)

    with pytest.raises(broker.QualificationMutationError):
        _invoke(cloudformation=cloudformation)

    assert len(cloudformation.describe_calls) == 1
    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"Stacks": []},
        {"Stacks": [_stack(), _stack()]},
        {"Stacks": ["not-a-stack"]},
    ],
)
def test_ambiguous_cloudformation_response_fails_closed(
    response: Any,
) -> None:
    cloudformation = FakeCloudFormation(
        _stack(),
        describe_response=response,
    )

    with pytest.raises(broker.QualificationMutationError):
        _invoke(cloudformation=cloudformation)

    assert len(cloudformation.describe_calls) == 1
    assert cloudformation.update_calls == []


def test_control_plane_must_link_to_exact_managed_agentcore_stack() -> None:
    stack = _stack(stack_kind="control-plane")
    _set_output(stack, "AgentCoreStackName", "AxonLLMAgentCoreStack")
    cloudformation = FakeCloudFormation(stack)

    with pytest.raises(broker.QualificationMutationError):
        _invoke(
            stack_kind="control-plane",
            cloudformation=cloudformation,
        )

    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    (
        "stack_kind",
        "legal_edge",
        "mode",
        "selected_table",
        "stack_approval",
    ),
    [
        ("agentcore", "quiesce-primary", "normal", RESTORED_TABLE, APPROVAL_ID),
        ("agentcore", "quiesce-primary", "selected", PRIMARY_TABLE, APPROVAL_ID),
        ("agentcore", "quiesce-primary", "quiesced", PRIMARY_TABLE, "CHG-OTHER"),
        ("agentcore", "cutover-to-restored", "normal", PRIMARY_TABLE, APPROVAL_ID),
        ("agentcore", "cutover-to-restored", "quiesced", RESTORED_TABLE, APPROVAL_ID),
        ("agentcore", "cutover-to-restored", "quiesced", PRIMARY_TABLE, "CHG-OTHER"),
        ("agentcore", "resume-restored", "quiesced", RESTORED_TABLE, APPROVAL_ID),
        ("agentcore", "resume-restored", "selected", RESTORED_TABLE, APPROVAL_ID),
        ("agentcore", "resume-restored", "selected", PRIMARY_TABLE, APPROVAL_ID),
        ("agentcore", "resume-restored", "selected", RESTORED_TABLE, "CHG-OTHER"),
        ("control-plane", "resume-restored", "quiesced", RESTORED_TABLE, APPROVAL_ID),
    ],
)
def test_illegal_graph_states_are_rejected(
    stack_kind: str,
    legal_edge: str,
    mode: str,
    selected_table: str,
    stack_approval: str,
) -> None:
    cloudformation = FakeCloudFormation(
        _stack(
            stack_kind=stack_kind,
            mode=mode,
            selected_table=selected_table,
            approval_id=stack_approval,
        )
    )

    with pytest.raises(broker.QualificationMutationError):
        _invoke(
            stack_kind=stack_kind,
            legal_edge=legal_edge,
            cloudformation=cloudformation,
        )

    assert cloudformation.update_calls == []


def test_describe_failure_is_wrapped_and_update_is_not_attempted() -> None:
    cloudformation = FakeCloudFormation(
        _stack(),
        describe_error=TimeoutError("network"),
    )

    with pytest.raises(
        broker.QualificationMutationError,
        match="could not be described",
    ):
        _invoke(cloudformation=cloudformation)

    assert cloudformation.update_calls == []


@pytest.mark.parametrize(
    "update_response",
    [
        None,
        [],
        {},
        {"StackId": CONTROL_PLANE_STACK_ARN},
    ],
)
def test_update_response_must_confirm_the_exact_stack(
    update_response: Any,
) -> None:
    cloudformation = FakeCloudFormation(
        _stack(),
        update_response=update_response,
    )

    with pytest.raises(broker.QualificationMutationError):
        _invoke(cloudformation=cloudformation)

    assert len(cloudformation.describe_calls) == 1
    assert len(cloudformation.update_calls) == 1


def test_update_failure_is_wrapped_without_a_retry_in_the_same_invocation() -> None:
    cloudformation = FakeCloudFormation(
        _stack(),
        update_error=TimeoutError("network"),
    )

    with pytest.raises(
        broker.QualificationMutationError,
        match="could not be started",
    ):
        _invoke(cloudformation=cloudformation)

    assert len(cloudformation.describe_calls) == 1
    assert len(cloudformation.update_calls) == 1


@pytest.mark.parametrize(
    "clock_value",
    [
        True,
        -1,
        float("inf"),
        float("nan"),
        "now",
    ],
)
def test_invalid_clock_fails_before_authorization_read(
    clock_value: Any,
) -> None:
    dynamodb = FakeDynamoDB(_authorization_item())
    cloudformation = FakeCloudFormation(_stack())

    with pytest.raises(broker.QualificationMutationError):
        broker.handle_event(
            _event(),
            dynamodb_client=dynamodb,
            cloudformation_client=cloudformation,
            environ=_environment(),
            clock=lambda: clock_value,
        )

    assert dynamodb.calls == []
    assert cloudformation.describe_calls == []


def test_lambda_handler_uses_injected_clients_without_global_state() -> None:
    dynamodb = FakeDynamoDB(_authorization_item())
    cloudformation = FakeCloudFormation(_stack())

    result = broker.lambda_handler(
        _event(),
        object(),
        dynamodb_client=dynamodb,
        cloudformation_client=cloudformation,
        environ=_environment(),
        clock=lambda: NOW,
    )

    assert result == {"status": "PENDING"}
    assert len(dynamodb.calls) == 1
    assert len(cloudformation.describe_calls) == 1
    assert len(cloudformation.update_calls) == 1
