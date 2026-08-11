# AxonLLM Features And Flows

This document inventories the implemented AxonLLM feature set and traces its
principal request and deployment flows. It describes repository behavior, not
a certified AWS environment. The query and shared control-plane implementation
has not yet been covered by a tagged release, deployed Athena canary, or
control-plane canary.

See also:

- [README](../README.md)
- [Product requirements](PRD.md)
- [AgentCore runbook](AGENTCORE_RUNBOOK.md)
- [Production runbook](PRODUCTION_RUNBOOK.md)
- [Enterprise hardening status](../ENTERPRISE_HARDENING.md)

## Runtime Surfaces

| Surface | Purpose | Query behavior |
|---|---|---|
| Normal Starlette process | Chat, OpenAI-compatible APIs, admin APIs, SAML/SCIM, and web interfaces | Registers `POST /v1/query` when Athena query configuration is enabled |
| AgentCore runtime | Authenticated action API for Bedrock-backed workloads | Exposes the `query` action when the shared `QueryService` is configured |
| Shared-state control plane | Managed-Cognito HTTPS Fargate service for tenant administration | Exposes admin and datasource routes; `AXON_CONTROL_PLANE_ONLY=true` suppresses chat, model, and query execution routes |
| Local seeded demo | Anonymous fictional-data evaluation | Development only; not a production or AgentCore deployment input |

The control-plane stack imports the AgentCore table, data KMS key, event
outbox, SNS topic, and CloudWatch event log. It creates no second authority
table. Its task role has no Athena or STS assume-role permission, so a control
plane compromise cannot invoke the query executor through that task.

## Feature Inventory

| Area | Implemented capabilities |
|---|---|
| Identity | OIDC/JWKS, ALB OIDC, API keys, SAML 2.0, SCIM 2.0, managed Cognito first-adopter identity |
| Tenant RBAC | Canonical DynamoDB principals, project grants, service scopes, viewer/admin roles, audited platform break glass |
| Query | Bounded Athena `SELECT`, HTTP and AgentCore entry points, deployment-bound IAM roles, datasource metadata administration |
| Routing | Thirteen provider adapters, retry/fallback, round-robin, weighted, least-latency, cost-optimized, smart, and ensemble paths |
| Chat | Native and OpenAI-compatible APIs, streaming, tool translation, model/project access checks |
| Governance | Policy hierarchy, Cedar restrictions, project/user budgets, quotas, shared rate and budget admission |
| Safety | PII redaction and re-injection, optional Comprehend entities, prompt-injection detection, request/response guardrails |
| Efficiency | Exact and semantic caches, prompt efficiency analytics, model right-sizing recommendations |
| Administration | Projects, users, keys, policies, quotas, regions, webhooks, audit, usage, datasource metadata, readiness and drift views |
| Durability | Tenant-qualified DynamoDB state, compare-and-swap updates, audit hash chains, API-key epochs, SCIM convergence |
| Events | FIFO outbox, bounded retries, DLQ, HTTPS/SNS/CloudWatch destinations, deterministic delivery identities |
| Operations | Fargate and AgentCore CDK, managed-Cognito shared control plane, immutable image inputs, backups, alarms, release evidence |

## Identity And RBAC Flow

Canonical production requests use server-held authority:

1. Middleware verifies exactly one supported credential source.
2. The verified issuer, subject, tenant hint, and project hint locate the
   canonical principal.
3. DynamoDB replaces credential-supplied roles, scopes, status, grants, and
   principal identity; it does not merge them.
4. AxonLLM strongly resolves the selected project under the canonical tenant.
5. Baseline RBAC evaluates the action, role, project grant, and any service
   scope.
6. Cedar policy may narrow an allow decision but cannot expand an RBAC denial.
7. Cross-tenant and ungranted resources are concealed as `404`; authority-store
   failures return `503`.

| Role | Tenant configuration | Datasources | Project data plane |
|---|---|---|---|
| `tenant_admin` | Read/write | Read/write | Model list, inference, and query with an explicit project grant |
| `tenant_member` | Read only | Read only | Model list, inference, and query with an explicit project grant |
| `tenant_auditor` | Read only | Read only | Model list, inference, and query with an explicit project grant |
| `service` | Denied | Denied | Explicit project grant plus server-held `model.list`, `inference.invoke`, or `query.select` scope |
| `platform_admin` | Platform resources; tenant access requires audited break glass | Break glass only | No ordinary tenant project path |

`query.mutate` remains an unconditional denial. There is no write-query action.

## Datasource Administration Flow

Datasource records contain metadata only: datasource id, tenant/project owner,
display name, exact IAM role ARN, AWS region, Athena catalog, database,
workgroup, enabled state, revision, and timestamps. They contain no AWS access
key, secret, session token, database password, or connection string.

| Endpoint | Behavior |
|---|---|
| `GET /admin/datasources` | List at most 100 tenant datasources using optional `project_id`, `limit`, and opaque `cursor` |
| `POST /admin/datasources` | Create metadata for an existing tenant project |
| `GET /admin/datasources/{datasource_id}` | Read one datasource; requires `project_id` |
| `PUT /admin/datasources/{datasource_id}` | Replace metadata using `expected_revision` |
| `DELETE /admin/datasources/{datasource_id}` | Delete using `project_id` and `expected_revision` query parameters |

Write flow:

1. Admin middleware requires canonical `tenant_admin` write authority.
2. The handler resolves the project under the request tenant.
3. The requested role ARN must match an exact deployment binding for that
   tenant and project.
4. A durable, redacted mutation-request audit record is appended; role ARNs
   appear only as SHA-256 values in change data.
5. DynamoDB applies revision compare-and-swap. Creates also increment the
   tenant's datasource counter in the same transaction; the default cap is 500.
6. A durable mutation-result audit record is appended.
7. A stale write or exhausted quota returns `409`; authority or audit failure
   returns `503`.

Members and auditors may read metadata but cannot mutate it. Their response
omits the role ARN and reports `role_configured: true`. Canonical service
principals are denied all `/admin/*` access.

## Query Security Contract

| Control | Enforced behavior |
|---|---|
| Enablement | No role bindings means the Athena query service is disabled |
| Binding | The datasource role must match an exact `(tenant_id, project_id, role_arn)` deployment tuple |
| Authorization | `query.select`, canonical project ownership, project grant, and service scope where applicable |
| SQL policy | `sqlglot` parses exactly one Athena query AST; only read-only `SELECT` forms are accepted |
| Rejected SQL | Multiple statements, DDL, DML, commands, `SELECT INTO`, table functions, and out-of-datasource catalog/database references |
| Workgroup | Must be enabled, enforce its configuration, publish CloudWatch metrics, use an enforced KMS-encrypted S3 result location, and set a positive scan cutoff no greater than the AxonLLM limit |
| Runtime bounds | Timeout, row count, compact serialized columns-and-rows result-set bytes, and bytes scanned; nonterminal work is cancelled on timeout or request cancellation |
| Fleet admission | Atomic principal/project RPM, expiring concurrency slots, and worst-case aggregate scan reservations; enforcement fails closed |
| Lifecycle | `request_id` is unique per tenant/project; accepted state, Athena execution id, and terminal outcome are durable |
| Audit | Durable request, result, and policy-rejection events; SQL is represented by SHA-256 rather than stored as a literal |

Default limits are 30 seconds, 1,000 rows, a 1 MiB compact serialized
columns-and-rows result set (including JSON structure and nulls), and 1 GiB
scanned. Deployment configuration may tighten or raise them only within the
validated application bounds. A request `max_rows` may only reduce the deployed
row ceiling. Admission defaults are 30 project RPM, 10 principal RPM, five
project slots, two principal slots, 5 GiB project scan reservation per minute,
and 2 GiB principal scan reservation per minute.

### IAM Contract

The AgentCore stack grants its runtime execution role `sts:AssumeRole`,
`sts:TagSession`, and `sts:SetSourceIdentity` only for the exact configured
datasource role ARNs. Its private STS endpoint applies the same allowlist, and
its Athena endpoint admits the same approved roles for the bounded Athena API
set.

The role name is deterministic:
`axonllm-agentcore-runtime-<region>`. The stack exports and the deployer prints
its full ARN as `RuntimeExecutionRoleArn`, so operators can configure exact
datasource-role trust before the first runtime deployment and verify that trust
against deployment output afterward.

Each datasource role must:

1. Trust the exact AgentCore runtime execution-role principal for
   `sts:AssumeRole`, `sts:TagSession`, and `sts:SetSourceIdentity`.
2. Permit only the intended catalog, database, tables, workgroup, result
   bucket, and KMS key.
3. Use an Athena workgroup satisfying the enforced configuration contract
   above.

AxonLLM assumes the role for 15 minutes, sends tenant/project session tags,
hashes the principal identifier before tagging it, and uses a hashed session
and source identity. Tags support attribution; canonical DynamoDB authority and
the deployment binding remain the authorization source.

## HTTP Query Flow

`POST /v1/query` runs only on a normal Starlette data-plane process with query
configuration enabled. It is not registered by a
`AXON_CONTROL_PLANE_ONLY=true` process.

```json
{
  "project_id": "project-a",
  "datasource_id": "warehouse",
  "sql": "SELECT order_id, status FROM orders LIMIT 100",
  "max_rows": 100,
  "request_id": "report-001"
}
```

`project_id` is optional, but when present it must equal the authenticated
project context. `datasource_id` and `sql` are required.

1. Starlette authentication and canonical authorization resolve the principal
   and project.
2. Middleware maps the request to `query.select`.
3. `QueryService` resolves the tenant/project datasource and exact role binding.
4. The AST policy validates and canonicalizes SQL.
5. A durable `query_request` audit event is appended.
6. Distributed rate, concurrency, and aggregate scan capacity is reserved;
   duplicate `request_id` values are rejected.
7. The executor assumes the bound role, validates the workgroup, and starts
   Athena.
8. The Athena execution id is durably attached to the lifecycle record before
   status polling.
9. Results are bounded; terminal lifecycle state reconciles the worst-case scan
   reservation and releases concurrency slots.
10. A durable success or failure `query_result` event is appended, then the
    response returns columns, rows, truncation, execution id, and statistics.

Malformed or unsafe SQL produces `query_rejected` before Athena starts.

## AgentCore Query Flow

The AgentCore payload selects the `query` action:

```json
{
  "action": "query",
  "datasource_id": "warehouse",
  "sql": "SELECT order_id, status FROM orders LIMIT 100",
  "max_rows": 100,
  "request_id": "report-001"
}
```

1. AgentCore's JWT authorizer and the AxonLLM verifier validate the OIDC token.
2. The adapter rejects payload-supplied tenant, project, role, or scope.
3. Canonical principal/project resolution and `query.select` authorization run.
4. Cedar evaluates the same logical `POST /v1/query` policy action used by
   HTTP.
5. The adapter invokes the same `QueryService` used by Starlette.
6. The response schema is validated before it leaves the AgentCore action.

The two entry points therefore share datasource lookup, SQL policy, role
binding, Athena limits, and durable audit behavior.

## Chat And Routing Flow

1. Authenticate and resolve canonical tenant/project authority.
2. Require `inference.invoke`, project membership, and allowed model access.
3. Reserve shared rate and budget capacity.
4. Validate the request, detect prompt injection, redact PII, and apply input
   guardrails.
5. Check exact cache, then optional semantic cache.
6. Select region and provider using the configured routing strategy.
7. Retry transient failures and traverse the fallback chain.
8. Translate provider requests, streaming chunks, tool calls, and responses.
9. Apply output guardrails and restore redacted values.
10. Persist usage/cost and audit state, dispatch threshold/security events, and
    return the normalized response.

Tool calling and the built-in query plane are separate. AxonLLM transports a
caller-defined `db_query` tool and its model-generated arguments, but does not
automatically execute that tool. A caller invokes `/v1/query` or the AgentCore
`query` action explicitly when it wants the governed Athena flow.

## Audit And Outbox Flow

Audit and event delivery serve different purposes:

| Mechanism | Contract |
|---|---|
| Audit trail | Tenant-qualified append-only records linked by SHA-256 hash chain |
| Query audit | Durable request/result/rejection records containing query hashes and bounded execution statistics, not SQL literals |
| Query lifecycle | Durable accepted/running/terminal state plus execution id; expiring distributed admission slots bound process loss |
| Datasource audit | Durable redacted mutation request/result records; raw role ARNs are excluded from change data |
| Security-event outbox | Tenant destination snapshot written to encrypted FIFO SQS before delivery |
| Delivery worker | Bounded retries to HTTPS, FIFO SNS, or CloudWatch Logs |
| DLQ | Retains a message after five failed receives for controlled redrive |

Destination configuration does not grant delivery authority beyond the stack's
managed SNS and CloudWatch allowlists. Receivers must be idempotent because
delivery is at least once.

## First-Adopter Deployment Flow

### Managed Cognito

`axon setup agentcore` writes the strict schema-version-2 setup contract
(`"schema_version": 2`). Managed Cognito requires its `control_plane` object.
Required inputs include:

- stable lowercase control-plane DNS name;
- regional ACM certificate and Route 53 public hosted-zone id;
- verified ARM64 AgentCore image digest;
- verified AMD64 control-plane image digest;
- exact Bedrock model/profile ARNs;
- AgentCore HTTPS egress prefix list;
- control-plane ingress and HTTPS egress prefix lists;
- optional exact Athena role ARNs and query limits.

`deploy-agentcore.sh` performs:

1. Deploy retained managed Cognito identity, including the AgentCore public
   client and confidential ALB client.
2. Invite or verify the first Cognito administrator.
3. Deploy AgentCore with the immutable ARM64 image and exact runtime authority.
4. Bootstrap and strongly verify the canonical tenant, project,
   `tenant_admin`, and project grant in the AgentCore table.
5. Deploy the HTTPS AMD64 `AxonLLMControlPlaneStack`, importing that same table
   and identity.

The control plane is private-task Fargate behind an HTTPS ALB with Cognito
authentication and a Route 53 alias. It is an administration surface, not a
second data plane.

### External OIDC

The external-IdP path verifies the supplied issuer, discovery URL, client,
audience, tenant/project claim names, and immutable first-admin subject. It
deploys AgentCore and bootstraps canonical authority.

It currently does **not** deploy the Cognito-authenticated shared web control
plane. Tenant configuration must be managed through another trusted Starlette
control plane sharing the state table, or the adopter must choose managed
Cognito.

## Known Boundaries

- AgentCore Memory and `SessionManager` remain unwired; conversations are not
  durably remembered by AgentCore.
- The shared control plane has no query execution route or Athena/STS authority.
- The external-OIDC first-adopter path has no automated web control plane.
- The existing `v0.2.4` evidence predates the query/control-plane work and does
  not certify it.
- No repository implementation substitutes for authenticated canaries,
  datasource-role trust validation, alarm delivery, load evidence, or a real
  AWS recovery rehearsal.
