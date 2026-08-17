# Ostiari Embedded Adapter

Status: AxonLLM adapter implemented locally; Ostiari consumer migration pending.

## Purpose

Ostiari can host the AxonLLM router in its own process without running another
HTTP service or creating AWS infrastructure:

```text
Ostiari authentication and governance
    -> OstiariRouterAdapter
    -> AsyncRouter
    -> selected model provider
```

AxonLLM owns provider selection, translation, retry, fallback, token accounting,
and cost calculation. Ostiari owns request identity, workflow governance,
verified configuration delivery, credential resolution, durable usage,
telemetry, and process lifecycle.

The adapter imports no Ostiari, Starlette, Uvicorn, FastAPI, boto3, or
control-plane persistence package.

## Public Contract

The public objects are:

- `OstiariHost`
- `OstiariRouterAdapter`
- `OstiariResult`
- `OstiariUsageRecordingError`
- `build_ostiari_adapter`

An Ostiari host structurally implements these methods:

```python
class Host:
    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def load_snapshot(self) -> RoutingConfigSnapshot: ...
    async def publish_snapshot(
        self,
        config,
        *,
        expected_revision: int,
    ) -> RoutingConfigSnapshot: ...

    async def resolve(
        self,
        *,
        provider: str,
        reference: str,
    ) -> Mapping[str, str]: ...

    async def record(self, usage: UsageRecord) -> None: ...
    async def emit(self, event: Mapping[str, object]) -> None: ...
```

`load_snapshot` and `publish_snapshot` must cryptographically verify the
snapshot before returning it. AxonLLM then binds the verified snapshot to the
configured signing-key ARN and rejects:

- unsigned snapshots;
- an unexpected signing key;
- revision rollback;
- two different documents with the same revision; and
- a published document that differs from the locally validated candidate.

## Startup And Shutdown

Create the router and adapter during Ostiari startup:

```python
from axonllm import build_ostiari_adapter, build_router

router = build_router(
    models="config/models.yaml",
    providers="config/providers.yaml",
    pricing="config/pricing.yaml",
)
axon = build_ostiari_adapter(
    router=router,
    host=ostiari_host,
    trusted_signing_key_arn=trusted_routing_key_arn,
)

await axon.start()
```

Ostiari must call:

```python
await axon.close()
```

from its lifespan shutdown path. Startup and shutdown are idempotent. Router
construction itself starts no server, database, AWS control-plane client, or
background worker.

## Request Identity

Ostiari resolves authentication and governance before calling AxonLLM:

```python
from axonllm import IdentityContext

result = await axon.route(
    messages,
    model=model,
    tools=tools,
    identity=IdentityContext(
        principal_id=agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        roles=frozenset(roles),
        scopes=frozenset(scopes),
    ),
    session_id=session_id,
)
```

AxonLLM does not derive identity from headers or environment variables in
embedded mode.

## Provider Credentials

Ostiari sends opaque references, never credential values:

```python
await axon.configure_provider_routes(
    [
        {
            "route_id": "openai:primary",
            "provider": "openai",
            "endpoint": "https://api.openai.com",
            "credential_reference": "vault://providers/openai/primary",
        }
    ]
)
```

The host resolves the reference at runtime. AxonLLM rejects non-empty inline
`credentials` fields and excludes resolved values from route snapshots,
telemetry, and errors. Workload-identity routes may omit a reference when no
secret value is required.

## Usage And Telemetry

Each successful call produces:

1. one normalized `UsageRecord` through `UsageSink.record`; and
2. one secret-free routing event through `TelemetrySink.emit`.

Telemetry is best effort and cannot fail a completed request.

Usage failure is different. `OstiariUsageRecordingError` contains the completed
`result`. Ostiari must not invoke a direct provider fallback or retry the model
call after catching this exception, because the provider has already completed
and may already have charged for the request.

The safe handling pattern is:

```python
from axonllm import OstiariUsageRecordingError

try:
    result = await axon.route(...)
except OstiariUsageRecordingError as exc:
    result = exc.result
    mark_accounting_degraded(exc.cause)
```

Ostiari's existing `CostReporter` must not also record an Axon-routed call.
Implement `UsageSink` using the canonical Ostiari usage path, and retain the
legacy reporter only for an explicitly selected direct-provider fallback.

## Migration From The Legacy Ostiari Adapter

The current Ostiari `AxonRouter` should be migrated in this order:

1. Add an Ostiari host facade implementing `OstiariHost`.
2. Replace `sys.path`, repository discovery, `os.chdir`, and
   `build_gateway_agent()` with `build_router()` and
   `build_ostiari_adapter()`.
3. Start and close the adapter from Ostiari's existing application lifespan.
4. Convert model-registry pushes into signed revision publication.
5. Convert provider routes from inline credentials to
   `credential_reference`.
6. Pass a canonical `IdentityContext` on every call.
7. Route Axon usage through the host `UsageSink` exactly once.
8. Treat `OstiariUsageRecordingError` as a completed provider call.
9. Keep direct-provider fallback explicit and observable. Production should
   normally require AxonLLM rather than silently weakening routing governance.
10. Remove private reads of `GatewayAgent`, `provider_fn_factory`,
    `model_registry`, and other `src.gateway` internals.

The Ostiari gateway repository currently has independent changes in its
lifecycle and server files. Apply this consumer migration only after those
changes are reconciled; do not overwrite them.

## Verification

AxonLLM tests prove:

- the adapter imports without Ostiari, server, or AWS packages;
- startup adopts only trusted signed snapshots;
- rollback and revision equivocation fail closed;
- provider credentials remain opaque;
- identity reaches usage and telemetry records;
- telemetry failure does not fail a request;
- usage failure preserves the completed result and causes no second provider
  invocation;
- shutdown closes both the router and host exactly once; and
- the shared embedded routing suite retains chat, fallback, tool, error, and
  usage behavior.
