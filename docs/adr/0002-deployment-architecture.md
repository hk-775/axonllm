# ADR 0002: AxonLLM Deployment Architecture

- Status: Accepted
- Date: 2026-08-16

## Context

AxonLLM must support three delivery modes with one routing implementation:

- an in-process router embedded in Ostiari;
- a standalone container with the gateway, control API, and UI; and
- an Amazon Bedrock AgentCore runtime with a separately hosted control plane.

The current AWS reference deployment creates dedicated networking for both the
AgentCore runtime and the Fargate control plane. That is useful for an isolated
reference environment, but it assumes every adopter needs AxonLLM-owned VPCs,
subnets, NAT gateways, and an Application Load Balancer. Many production
adopters already have approved networking, egress, DNS, identity, and
monitoring.

The current package and process boundaries are also wider than the product
needs. Embedding the router should not initialize a web server, AWS control
plane, persistence layer, or background workers.

## Decision

AxonLLM will use one infrastructure-neutral router core with three host
adapters:

1. Ostiari embeds the router and supplies configuration, identity, telemetry,
   usage, and credential interfaces.
2. Standalone installations use one multi-architecture image containing the
   router, control API, and browser UI.
3. AgentCore installations place only the routing data plane in AgentCore.
   Static UI assets are served through CloudFront and S3, while request-driven
   control APIs and workers use Lambda, SQS, and EventBridge.

AgentCore deployment configuration is explicit and schema-versioned. Version 1
of the deployment schema covers the AgentCore target. Infrastructure-neutral
Ostiari host protocols and explicit router/control/worker construction seams
are part of the package contract. Standalone recipes and the production
Ostiari adapter will be added without forcing either into AgentCore-specific
fields.

The AgentCore network modes are:

- `existing`: use customer-owned VPC, subnet, security-group, and egress
  resources without changing their lifecycle;
- `managed`: create a dedicated network only after the adopter selects it; and
- `public`: development only.

Egress is independent from VPC ownership:

- `endpoints-only` for configurations whose dependencies are privately
  reachable;
- `existing-egress` for customer-managed NAT, firewall, proxy, or centralized
  egress; and
- `managed-nat` only with an explicit cost acknowledgement.

Durable application state and identity will be separated from disposable
runtime and optional managed-network stacks. `park` and `resume` will become
supported lifecycle operations rather than one-off template surgery.

Customer access never depends on port forwarding. Public authenticated
deployments use an HTTPS CloudFront endpoint. Private enterprise deployments
use connectivity supplied by the adopter.

## Consequences

- Existing production infrastructure remains authoritative until each
  migration gate passes.
- The initial implementation is contract-first: schema, examples, validation,
  and a non-mutating planner precede stack refactoring.
- Customer-owned network resources are referenced but never adopted into
  AxonLLM lifecycle management.
- The AgentCore UI target contains no ALB, ECS service, UI VPC, or UI NAT
  gateway.
- Bedrock-only does not automatically mean zero egress; preflight must prove
  every enabled dependency is reachable.
- The standalone image may co-locate components, but AgentCore and Ostiari are
  not required to copy that process shape.
- Moving stateful CDK constructs or changing their logical IDs is prohibited
  until replacement guards and an explicit import/refactor plan are in place.

## Verification

The implementation must prove:

- unknown deployment configuration fields fail closed;
- production rejects public runtime networking;
- managed NAT requires explicit acknowledgement;
- `existing` mode synthesizes no VPC, subnet, NAT, internet gateway, route
  table, or VPC endpoint;
- retained state cannot be replaced or deleted by runtime lifecycle changes;
- the Ostiari adapter initializes no server or AWS infrastructure client;
- the standalone and AgentCore adapters pass the shared routing conformance
  suite; and
- parking removes only disposable runtime and optional managed-network
  resources.

The detailed migration sequence and acceptance gates are defined in the
[Deployment Architecture Plan](../DEPLOYMENT_ARCHITECTURE_PLAN.md).
