# AxonLLM Standalone Runbook

This runbook covers the one-image AxonLLM deployment. The image serves the
gateway, control API, and browser UI on port `8000`. It does not create a VPC,
subnet, NAT gateway, load balancer, database, identity provider, or container
cluster.

The checked-in implementation is qualification-ready, but a source checkout is
not a certified release. Use only an immutable image digest that passed the
signed release and deployment-verification workflows.

## Deployment Shapes

| Shape | Purpose | State and ingress |
|---|---|---|
| Evaluation Compose | Disposable local evaluation | Seeded local state and localhost |
| Production Docker | Customer-managed host or scheduler | External network, identity, secrets, and durable state |
| Existing ECS/Fargate | AWS container deployment without Axon-owned networking | Existing cluster, private subnets, security groups, target group, identity, and state |

The evaluation profile is never promoted. Production always starts through
`python -m src.gateway.standalone`, with authentication enforced, canonical
identity required, and demo data disabled.

## Select The Signed Image

Release evidence schema v4 has two standalone targets:

| Target | Platform | Repository tag |
|---|---|---|
| `standalone-amd64` | `linux/amd64` | `vX.Y.Z-amd64` |
| `standalone-arm64` | `linux/arm64` | `vX.Y.Z-arm64` |

The tags are operator conveniences. Deployment inputs use the returned
`repository@sha256:...` identity.

The release foundation owns the retained, KMS-encrypted, immutable
`axonllm/standalone` ECR repository. Configure the protected GitHub `release`
environment variable `AXON_STANDALONE_ECR_REPOSITORY` from the
`StandaloneRepositoryUri` stack output.

Before deployment:

1. Require a successful tagged `release-security.yml` run for the exact commit.
2. Require a successful `deploy-verification.yml` run for the selected
   standalone platform target.
3. Record the evidence run ID, commit, tag, target, image digest, and signing
   key ARN.
4. Reject mutable tags, platform mismatches, or a digest that differs from the
   signed target.

## Evaluation

The repository root Compose file explicitly enables the disposable development
profile:

```bash
docker compose up --build
```

Do not expose this profile to a shared network. It uses seeded fictional data
and `LOG_ONLY` authentication.

## Production Docker

The production recipe joins an existing external network:

```bash
docker compose \
  -f deploy/standalone/compose.production.yml \
  config
```

Supply all required values through the container platform. Do not place secret
values in the Compose file, deployment plan, image, shell history, or source
repository.

Required production ownership:

- digest-pinned standalone image for the host architecture;
- customer-owned TLS ingress and network;
- external DynamoDB state and KMS keys;
- exact OIDC issuer and audience;
- task or workload identity with least-privilege state/provider access;
- provider credentials from the platform secret store; and
- durable logs, alarms, backups, and recovery procedures.

The container must run as its packaged non-root user with a read-only root
filesystem, dropped Linux capabilities, `no-new-privileges`, and writable
temporary storage only under `/tmp`.

## Existing ECS/Fargate

Create a local, read-only deployment plan:

```bash
axon deploy standalone-plan \
  --context config/deployment/standalone-ecs-existing.example.json \
  --output-dir .axon/plans
```

The context must identify existing resources only:

- ECS cluster;
- private subnets;
- task security groups;
- IP target group;
- task and execution roles;
- state table and KMS keys;
- log group;
- identity configuration; and
- secret ARNs.

The planner emits content-addressed task-definition and service specifications.
It cannot call AWS, register a task definition, or update a service.

Review the plan and reject it if:

- the image is not an exact same-account private ECR digest;
- public IP assignment is enabled;
- the task and execution roles are the same;
- secrets appear as plaintext environment variables;
- the root filesystem is writable;
- the service has fewer than two production tasks;
- circuit-breaker rollback is disabled;
- the target group is not type `ip`;
- the health path is not `/ready`; or
- any VPC, subnet, route, gateway, load balancer, database, or identity resource
  would be created.

Registration and service update remain separately reviewed operations.

## Preflight

Before starting or updating production:

1. Confirm the image architecture matches the host.
2. Confirm the image digest matches signed release evidence.
3. Confirm private subnets have approved provider and AWS-service egress.
4. Confirm the task security group accepts port `8000` only from customer
   ingress.
5. Confirm DynamoDB PITR, deletion protection, encryption, TTL, and backup
   ownership.
6. Confirm OIDC discovery and JWKS are reachable.
7. Confirm provider secrets exist without retrieving their values.
8. Confirm log delivery, alarms, and incident subscribers.
9. Confirm rollback can restore the previous task-definition digest.

## Health And Canaries

Use separate signals:

- `/health`: process liveness;
- `/ready`: state, identity, and required dependency readiness;
- authenticated `/v1/models`: routing catalog;
- one authenticated non-streaming request;
- one authenticated streaming request;
- dashboard login and one read-only administration request;
- one denied cross-tenant request; and
- one usage and audit record for the successful request.

Do not send traffic merely because the process is alive. The load balancer
must use `/ready`.

## Upgrade

1. Preserve the current digest, task definition, service settings, and state
   identifiers.
2. Verify the new platform target against signed release evidence.
3. Generate a new standalone plan with only the image digest and explicitly
   reviewed settings changed.
4. Reject state, role, network, target-group, or secret-identity drift.
5. Use a rolling service update with circuit-breaker rollback.
6. Run all readiness and authorization canaries.
7. Retain the prior digest and task definition through the rollback window.

## Rollback

Rollback changes the service back to the previous verified task definition.
It does not change or restore the state table during a normal application
rollback.

If a new release wrote incompatible state, stop the rollout and use the
separately rehearsed data-recovery procedure. Do not improvise a table switch
inside the service update.

## Stop And Remove

Scale or remove standalone compute through the customer-owned scheduler.
Retain state, keys, secrets, backups, logs, and release evidence according to
their independent lifecycle policies.

Never delete shared customer networking or identity resources as part of
AxonLLM removal.
