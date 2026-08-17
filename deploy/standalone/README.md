# Standalone Deployment

AxonLLM standalone mode serves the routing APIs, control API, and browser UI
from one image. It does not create a VPC, subnet, NAT gateway, load balancer,
database, identity provider, or container cluster.

See the full [Standalone Runbook](../../docs/STANDALONE_RUNBOOK.md) for signed
image selection, preflight, canaries, upgrades, rollback, and removal.

## Evaluation

The repository root `docker-compose.yml` explicitly selects the development
profile, `LOG_ONLY` authentication, and seeded demonstration data:

```bash
docker compose up --build
```

This profile is disposable and must not be exposed to a shared network.

## Existing Docker Infrastructure

The production Compose recipe joins a customer-owned external network and
requires a digest-pinned image plus external identity and DynamoDB state:

```bash
docker compose \
  -f deploy/standalone/compose.production.yml \
  config
```

Provider credentials must be injected by the container platform. Do not place
them in this repository or bake them into the image.

## Existing ECS/Fargate Infrastructure

Create a read-only plan from explicit, non-secret identifiers:

```bash
axon deploy standalone-plan \
  --context config/deployment/standalone-ecs-existing.example.json \
  --output-dir .axon/plans
```

The plan:

- requires an exact same-account private ECR digest;
- accepts an existing cluster, private subnets, security groups, and IP target
  group;
- requires existing DynamoDB, KMS, IAM, logging, and OIDC resources;
- creates no networking resources;
- emits a hardened `register-task-definition` input;
- specifies two or more Fargate tasks, `awsvpc`, public IP disabled, deployment
  rollback, blocking logs, read-only root filesystems, and `/ready` ingress
  health; and
- contains no AWS execution or registration path.

Registering the task definition and creating or updating the service remain
separately reviewed operations. Before applying, verify the target group uses
target type `ip`, its health check is `/ready`, the task security group accepts
port `8000` only from customer ingress, and the private subnets have approved
provider and AWS-service egress.
