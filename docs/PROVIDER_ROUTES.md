# Provider Route Pools

AxonLLM selects providers at two levels:

1. The existing model router chooses a logical model and provider.
2. The provider route pool chooses the concrete credential, endpoint, and
   transport used for that provider attempt.

A route is one independently observable execution path:

```text
route_id
provider
endpoint and region
credential
allowed models
static weight and priority
route and shared-account capacity
transport limits
```

Multiple credentials that consume the same provider-account quota must use the
same `capacity_group` and `capacity_limit`. Separate keys do not imply separate
provider capacity.

## Selection

Only enabled routes that allow the selected model and have available capacity
are eligible. AxonLLM first keeps routes at the lowest configured priority, then
uses adaptive weighted random selection inside that priority:

```text
effective weight =
  static weight
  * reliability factor
  * token-adjusted latency factor
  * available-capacity factor
  * recovery factor
```

The runtime maintains exponentially weighted error and latency signals for each
route. A route leaving cooldown receives a small exploration share until two
successful requests restore it to healthy.

Failures have route-specific effects:

| Result | Route handling |
|---|---|
| `401`, `402`, `403` | Quarantine the credential for five minutes |
| `429` | Cool down the route for 30 seconds when another route is available |
| Network error or `5xx` | Degrade immediately; cool down after repeated failure when a sibling route exists |
| `404` | Degrade that route because endpoint/model availability may differ |
| `400`, `405`, `409`, `422` | Do not damage route health; these normally describe the request |

For non-streaming calls, provider/model fallback remains the Router's
responsibility. The route pool does not add an independent retry budget. A
retryable failure lets the Router make its next attempt, which may select
another route or provider.

True streaming opens routes directly because the Router cannot retry an
iterator. It may rotate through eligible routes only before the first response
chunk, at most once per configured route. After any chunk is emitted, a failure
is returned to the caller and is never replayed.

## Connection Pools

HTTP sessions are keyed by transport identity:

- endpoint scheme, host, and port;
- proxy and TLS identity;
- timeout policy;
- connection and keepalive limits.

Credentials are added per request. Two API keys using the same endpoint and
transport policy therefore share one TCP/TLS pool. Routes with different
regions, proxies, TLS identities, or pool policies use different pools.

Bedrock and Bedrock Mantle clients are cached by route fingerprint and region so
credential or endpoint rotation creates a new client without sharing stale
state.

## Configuration

Legacy provider configuration still creates one `<provider>:default` route.
Use `routes` to define more:

```yaml
providers:
  openai:
    base_url: https://api.openai.com
    auth_type: api_key
    routes:
      - route_id: openai:primary
        api_key_env: OPENAI_PRIMARY_API_KEY
        weight: 3
        max_concurrency: 80
        capacity_group: openai-account-a
        capacity_limit: 100
      - route_id: openai:backup
        api_key_env: OPENAI_BACKUP_API_KEY
        weight: 1
        max_concurrency: 40
        capacity_group: openai-account-b
```

Supported route fields are:

| Field | Meaning |
|---|---|
| `route_id` | Stable unique route identity |
| `endpoint` / `base_url` | Provider endpoint; defaults to the provider standard |
| `auth_type` | `api_key`, `azure_key`, `aws_credentials`, or `gcp_service_account` |
| `allowed_models` | Optional provider-side model allowlist |
| `weight` | Static share before adaptive factors |
| `priority` | Lower values are preferred; higher values are failover tiers |
| `max_concurrency` | Per-route in-flight limit |
| `capacity_group`, `capacity_limit` | Shared-account in-flight limit |
| `connect_timeout`, `read_timeout` | Route transport deadlines |
| `max_connections`, `max_connections_per_host` | TCP pool limits |
| `keepalive_timeout` | Idle connection retention |

`MultiProviderFactory.configure_routes()` atomically replaces the catalog for
future requests. In-flight requests retain their existing route lease. Health is
preserved only when a route's fingerprint is unchanged; rotating a credential,
endpoint, model set, or capacity group resets stale health.

`MultiProviderFactory.route_snapshot()` returns route health, load, and adaptive
weights without credentials, custom headers, or private parameters.

## Ostiari Operation

When AxonLLM is embedded in Ostiari, Ostiari is the durable desired-state owner:

- route records and private material are tenant-scoped;
- private material is encrypted at rest;
- operators create, update, disable, and push routes from the Providers page;
- gateways hot-reload the complete catalog without a restart;
- only the authenticated gateway configuration channel carries resolved secrets;
- runtime snapshots returned to operators are secret-free.

AxonLLM owns the process-local measurements because latency, connection
saturation, and endpoint reachability differ between gateway instances.
