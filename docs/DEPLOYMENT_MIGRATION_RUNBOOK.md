# AxonLLM Deployment Migration Runbook

This runbook governs migration from the legacy combined AgentCore/Fargate
topology to the accepted deployment architecture:

- retained application state and identity;
- optional independently owned managed networking;
- disposable AgentCore runtime;
- static CloudFront/S3 UI;
- API Gateway/Lambda control API;
- request-driven Lambda workers; and
- the existing customer-facing CloudFront hostname.

This document does not authorize a production mutation. Every live step
requires a reviewed CloudFormation change set and explicit execution approval.

## Non-Negotiable Rules

1. Never combine state-resource import or ownership movement with a property
   update.
2. Never accept a retained-resource replacement or deletion.
3. Never delete AgentCore, ENIs, subnets, security groups, or stacks directly
   outside CloudFormation.
4. Never switch traffic before both old and new control planes pass the same
   state, identity, RBAC, session, audit, and export canaries.
5. Preserve the existing CloudFront distribution, hostname, WAF association,
   and Cognito callback.
6. Keep the Fargate origin available through the rollback window.
7. Change the production backend with only the reviewed
   `EdgeBackendMode=fargate|serverless` selector.
8. Retire hourly infrastructure only after rollback has been exercised and the
   billing inventory proves it is unused.

## Phase 0: Capture The Baseline

Record:

- account and region;
- source commit and signed image/artifact evidence;
- live templates, parameters, stack IDs, execution roles, and drift;
- logical and physical IDs for every retained resource;
- CloudFront distribution, WAF, hostname, origins, cache policies, and
  viewer-request function;
- Cognito clients, callback URLs, and enterprise IdP configuration;
- state table, keys, secrets, queues, topics, logs, backup vault, and recovery
  points;
- AgentCore runtime, endpoints, image digest, configuration revision, VPC,
  subnets, security groups, endpoints, NAT gateways, and service ENIs;
- Fargate service, task definition, ALB, target groups, and autoscaling; and
- current cost, latency, availability, alarms, and rollback owner.

Abort if stack drift is unresolved or the current deployment cannot be
reconstructed from recorded inputs.

## Phase 1: Establish Retained State Ownership

Synthesize `AxonLLMApplicationStateStack` with the migration-bound physical
names from the retained-resource manifest.

The state migration operation must contain:

- no resource property changes;
- no additions beyond migration bookkeeping;
- no replacements;
- no deletions;
- no unrelated IAM changes; and
- exact before/after logical and physical identity evidence.

Import or refactor one ownership boundary at a time. Verify DynamoDB, KMS,
Secrets Manager, SQS, SNS, logs, backup vault, backup plan, and roles after
each operation.

Do not proceed until both the runtime and control-plane templates consume the
same account/region-bound state descriptor and contain no new
`Fn::ImportValue` coupling.

## Phase 2: Select Networking

Choose one explicit runtime mode:

- `existing`: customer VPC, private subnets, security groups, and egress;
- `managed`: independent `AxonLLMManagedNetworkStack`; or
- `public`: namespaced development only.

For existing networking, run the read-only preflight and require:

- VPC DNS support;
- correct account and region;
- private subnet ownership;
- AgentCore-supported Availability Zone IDs;
- approved route ownership and egress;
- required VPC endpoints for endpoints-only mode; and
- unchanged customer resources before and after deployment.

Bedrock/AWS-only managed deployments create no NAT. External-provider egress
must be an explicitly acknowledged customer path or managed NAT configuration.

## Phase 3: Deploy Serverless Resources In Parallel

Publish and verify exact-version serverless artifacts. Deploy:

- `AxonLLMServerlessWorkersStack`;
- `AxonLLMServerlessControlPlaneStack` in isolated qualification mode; and
- the dedicated Cognito browser client and private static origin.

The new stacks must create no VPC, subnet, NAT gateway, ALB, or ECS service.

Verify:

- exact S3 object versions and SHA-256 digests;
- private static bucket access only through CloudFront;
- API Gateway origin-key rejection for direct requests;
- least-privilege Lambda roles;
- FIFO retry/DLQ behavior;
- asynchronous export creation, status, and download;
- scheduled reconciliation only after datasource roles trust its exact role;
  and
- no mutation of the production distribution.

## Phase 4: Qualify Both Control Planes

Produce passing production-validation reports for:

- legacy Fargate control plane; and
- isolated serverless control plane.

Both reports must bind the same:

- account and region;
- source revision;
- canonical state descriptor;
- identity stack and issuer;
- routing/configuration revision;
- release evidence; and
- tenant/project fixtures.

Required canaries include:

1. browser PKCE login and callback;
2. opaque session renewal and logout;
3. CSRF rejection and success paths;
4. canonical RBAC and cross-tenant denial;
5. SAML login when configured;
6. SCIM authorization and reconciliation;
7. configuration read and reversible mutation;
8. budget, usage, and audit parity;
9. security-event delivery and DLQ exercise;
10. asynchronous usage/audit export; and
11. control-plane outage with runtime last-known-good routing.

Create `axon deploy edge-plan` only from those passing reports and the exact
reviewed CloudFront-only change set.

## Phase 5: Prepare The Existing Edge

The preparation change set is allowed to:

- add one CloudFront origin access control;
- modify the existing CloudFront distribution to register the qualified S3 and
  API Gateway origins; and
- modify the existing viewer-request function to support the selector.

It must leave `EdgeBackendMode=fargate`.

Reject any change to state, identity, AgentCore, ECS, ALB, VPC, WAF, hostname,
certificate, or retained logs.

After execution, rerun the Fargate canaries through the unchanged customer URL.

## Phase 6: Cut Over

Create a second edge-only change set with:

```text
EdgeBackendMode: fargate -> serverless
```

Execute only after:

- both origins remain healthy;
- the edge plan hash matches the reviewed inputs;
- the rollback change set or command is prepared;
- operators and incident owners are present; and
- the rollback deadline is recorded.

Immediately run the complete browser, API, identity, RBAC, audit, export, and
runtime-independence canaries through the existing customer hostname.

## Rollback

Rollback is:

```text
EdgeBackendMode: serverless -> fargate
```

It must not change the state table, AgentCore runtime, Cognito pool, WAF,
hostname, or either origin stack.

Rollback immediately for:

- login/callback failure;
- elevated control API errors or latency;
- state or authorization mismatch;
- audit or usage loss;
- export/worker retry growth;
- origin-key bypass;
- customer-visible static asset failure; or
- inability to complete the full canary set.

After rollback, preserve both failure and rollback evidence. Do not delete the
serverless stacks while investigating.

## Phase 7: Close The Rollback Window

Keep both origins for the approved observation period. Require:

- sustained production canaries;
- no unresolved alarms or DLQ messages;
- no state, audit, or identity divergence;
- successful rollback rehearsal;
- acceptable latency and cost;
- signed migration and rollback receipts; and
- explicit owner approval to retire legacy compute.

Static-cache optimization is a later change. Do not combine it with cutover.

## Phase 8: Retire Legacy Fargate Infrastructure

Retirement uses a separate CloudFormation change set. It may remove:

- Fargate service and task definition resources;
- ALB, listeners, and target groups;
- the legacy control-plane VPC, subnets, NAT gateways, endpoints, and security
  groups; and
- legacy-only alarms and logs whose retention period has ended.

It must retain:

- the existing CloudFront distribution, WAF, hostname, and active serverless
  origins;
- application state, identity, keys, secrets, queues, audit logs, and backups;
- AgentCore runtime/network stacks;
- release, qualification, migration, and rollback evidence; and
- any shared customer resource.

Before execution:

1. Prove CloudFront has no active route to the Fargate origin.
2. Prove no healthy or draining target receives traffic.
3. Prove no other stack imports or references the resources.
4. Review every removal and reject replacements.
5. Record before/after hourly-cost inventory.

After execution, verify that NAT gateways, ALB capacity, ECS tasks, and legacy
VPC endpoints are gone.

## AgentCore Park And Resume

Park and resume are independent of control-plane migration.

- Park the AgentCore runtime stack first.
- Wait for AgentCore-managed ENIs to be released.
- Park the managed-network stack second.
- Resume the managed-network stack first.
- Resume the runtime stack second with the same signed image and configuration.

Use `axon deploy lifecycle-plan` and prepare-only change sets. After execution,
use `axon deploy lifecycle-receipt` to prove runtime/network state, retained
stack hashes, and control-plane availability.

AgentCore ENI cleanup may take hours. Do not manually delete service-managed
ENIs, subnets, or security groups.

## Completion Criteria

Migration is complete only when:

- retained resources have stable ownership and no replacement;
- the existing customer URL serves the serverless control plane;
- AgentCore routing remains independent of control-plane availability;
- rollback to Fargate has been rehearsed;
- Fargate/ALB/UI-VPC hourly resources are removed;
- park/resume is rehearsed through reviewed change sets;
- standalone platform images have signed release evidence; and
- all receipts and runbooks are retained with the release.
