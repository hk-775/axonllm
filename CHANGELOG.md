# Changelog

All notable changes to AxonLLM will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **All pinned workflow actions now run natively on Node 24.** Artifact
  upload/download, AWS credential configuration, and Docker QEMU/Buildx setup
  actions were upgraded to their Node 24 majors, removing GitHub's forced
  compatibility runtime from release, publication, deployment-verification,
  and operations workflows.

## [0.2.5] - 2026-08-11

### Added

- **AgentCore first-adopter identity and deployment workflow.** `axon setup
  agentcore` now writes a strict, mode-0600 configuration for either retained
  managed Cognito or an existing OIDC provider; mutable image tags, wildcard
  Bedrock ARNs, missing tenant/project claim mappings, client secrets, and
  unauthenticated production are rejected. `AxonLLMIdentityStack` provides
  admin-only Cognito enrollment, required TOTP, a strong password policy, and a
  secretless authorization-code client with retained resources and deletion
  protection. `deploy-agentcore.sh` deploys identity when selected, invites or
  verifies the first administrator, deploys the runtime, and idempotently
  verifies canonical tenant authority. Anonymous seeded use is separately
  labeled and requires `axon setup local-demo --acknowledge-non-production`.
- **Bounded Athena query and shared control plane.** The Starlette
  gateway and AgentCore runtime now share a credential-free, tenant-scoped
  `SELECT` service with SQL validation, admission limits, canonical project
  authorization, and durable audit records. Admin-only datasource management
  is exposed through HTTP, and `AxonLLMControlPlaneStack` provides the separate
  authenticated administration surface that the AgentCore runtime
  intentionally does not mount.

### Security

- **Deployment verification now scans the exact signed target platform.**
  Multi-architecture ECR references are resolved to the platform recorded in
  release evidence before Trivy rescans the deployment image.
- **GitHub workflows now use first-party actions built for Node 24.**
  `actions/checkout`, `actions/setup-python`, and `actions/setup-node` are
  upgraded across CI, release, deployment-verification, and operations
  workflows while remaining pinned to immutable commit SHAs.

### Fixed

- **A recovery cutover left the primary state table writable.** The Fargate task
  role always retained its stack-managed table grant while a second conditional
  policy added the restored table. State access now resolves through the same
  CloudFormation condition as `AXON_DYNAMODB_TABLE`, so the role can reach only
  the selected table and its indexes.
- **The ensemble concurrency property depended on CI wall-clock timing.** It now
  verifies panel and judge coroutine ordering directly, preserving the
  concurrency regression check without runner-contention failures.

### Documentation

- Added the AgentCore query and control-plane flows to the feature catalog,
  deployment runbooks, hardening assessment, PRD, and PRFAQ, and corrected
  production-validation status to distinguish retained release evidence from
  resources that are actually deployed.

## [0.2.4] - 2026-08-10

### Documentation

- **Documented how admin aggregates read on a multi-instance deployment**, now
  that they are fleet-wide (see Fixed). The shared-state table previously claimed
  "Usage/cost records ✅ shared", which was true of the write and false of the
  read; it now describes the two sources separately — costs exact from the shared
  counter, counts refreshed on a 10-second window — and states the
  `MAX_RECORDS` ceiling that makes count-based aggregates under-report on a busy
  fleet while cost figures stay correct.
- **A `docker compose` port clash on 8000 is now avoidable and explained.** The
  host port was the only value in `docker-compose.yml` not parameterised, and the
  clash is silent rather than fatal: Docker binds `::` while a local
  `serve_dashboard.py` binds `0.0.0.0`, so both start, `localhost` resolves to
  `::1` first, and the container answers for the gateway started by hand. Now
  `${AXON_HOST_PORT:-8000}`, documented next to the quickstart command.
- **Stated a minimum Node version for the CDK deploy.** The prerequisite said
  only "Node.js installed". On Node 18 every `cdk` call prints a ten-line
  end-of-life banner that reads like a failure and buries the real output, and
  the bundled AWS SDK warns it will require Node 22 from January 2027. Now
  documents 20+, preferring 22 or 24.

### Fixed

- **A project or per-user config written through the API gated only the task that
  served the write.** `self.projects` and `self._user_configs` are hydrated once at
  startup and thereafter mutated only by the instance that took the write. Both
  dicts *gate* requests, so unlike a stale count this fails open: an unresolved
  project means no budget limit, no allowed-models list and no rate limit, and a
  missing user config means no per-user model restriction. The restriction an
  operator set was enforced by one task and ignored by the other, chosen per
  request by the load balancer:

  ```
  in the store: {'alice': {'allowed_models': ['claude-haiku']}}
  task A, alice asks for claude-opus: 403 model_not_allowed
  task B, alice asks for claude-opus: 200 routed
  ```

  Fixed the same way as the Cedar divergence: config writes bump a shared version
  counter, and each instance re-reads the two config scans only when that number
  moves. Steady-state cost is one small `GetItem` per instance per 5-second window.
  Adopting the dicts is not the same as arming enforcement, so the refresh also
  re-registers budgets with `CostTracker` — the distinction the previous release
  fixed for the restart path, which applies identically here. As with the policy
  refresh, a failed config scan is **not** adopted as empty (that would clear every
  limit in the fleet because one read timed out), a failed write does not bump the
  version, and an unreadable counter does not advance the clock.
- **`GET /api/users` reported one task's view of usage while `/admin/*` reported
  the fleet's.** The previous release made the admin aggregates re-read the shared
  usage store but left the chat UI's user selector reading `cost_tracker._records`
  directly, so the same deployment answered the same question two ways depending on
  the endpoint. The 10-second refresh now lives on `CostTracker` rather than in
  `AdminAPI`, which gives both callers one clock and one in-flight scan instead of
  two of each.
- **Setting a user's budget through the admin API recorded it in DynamoDB and not
  in the running process, and the next write for that user erased it.**
  `self._user_configs.get(user_id, {})` hands back a throwaway dict on a miss, and
  the limits were written into that. Because `save_user_config` is a whole-item
  `put_item`, the *following* write for the same user then serialized a config with
  no budget in it — so the stored limit disappeared too. `setdefault` now, and the
  local dict is updated whether or not persistence is enabled, since the local dict
  is what the request path reads.
- **A Cedar policy written through the API governed only the task that served the
  write.** Statements are compiled once, so `POST /admin/policies` recompiled the
  local evaluator and nothing else. Behind the shipped `desired_count=2` an
  operator's `forbid` was enforced by one task and ignored by the other, decided
  per request by the load balancer:

  ```
  task A denies DELETE: DENY
  task B denies DELETE: ALLOW
  task B's policy list: []
  in the store: ['no-delete']
  ```

  Same shape as the admin-read divergence above, but it fails open on an
  authorization control rather than misreporting a number, and it does not
  self-correct: the policy is in the table, so a restart fixes it and nothing
  short of one does. `GET /admin/policies` on the other task also reported the
  policy missing, which reads as a failed write and invites writing it again.

  Writes now bump a shared version counter; each instance reads that counter at
  most once every 5 seconds and re-scans the policy table only when it moves. The
  steady-state cost is one small `GetItem` per instance per window rather than a
  scan per request, single-flighted so a burst of concurrent requests cannot each
  trigger their own.

  Three ways this fix could have been worse than the bug, all closed and tested:
  a failed policy scan is **not** adopted as an empty set (that would convert one
  timed-out read into a fleet-wide bypass — `load_all_cedar_policies` returns `[]`
  on failure, which is right for startup and catastrophic for a live reload); a
  failed *write* does not bump the version, so the fleet is not told to reload a
  change that never landed; and a refresh merges the stored set over the seeded
  one by name instead of replacing it, since `demo_seed.yaml` policies are not in
  DynamoDB and adopting the stored set wholesale silently un-enforced every one of
  them.
- **`AdminAPI` and `GatewayAgent` stopped sharing state when a container was
  empty.** `self.projects = projects or {}` substitutes a *new* dict whenever the
  caller passes an empty one, and an empty dict is falsy — so on any gateway
  booting without demo seed data, which is the production path, the admin API and
  the request path held different objects:

  ```
  no seed projects:  admin sees ['acme'], the dict the agent holds sees []
  one seed project:  admin sees ['acme', 'seed'], the dict the agent holds sees ['acme', 'seed']
  ```

  `POST /admin/projects` returned 201 for a project no chat request could resolve
  until restart. Found while fixing the policy divergence above, where the same
  expression broke the policy list the evaluator refreshes into. Fixed for
  `projects`, `policies`, and `user_configs` on both classes — the seeded path
  worked, which is why it survived: the bug is only reachable through the falsy
  case.
- **Budgets set through the admin API stopped being enforced after a restart.**
  `PUT /admin/users/{id}/budget` and `POST`/`PUT /admin/projects/{id}` write the
  limit to DynamoDB, and the next boot read it back into the `projects` and
  `user_configs` dicts — but the limits that `check_budget` and
  `check_user_budget` consult live in `CostTracker._budgets` / `._user_budgets`,
  and the one `QuotaEnforcer` resolves lives in the policy hierarchy's nodes.
  None of those is a dict a `.update()` reaches. `_apply_seed_data` registered
  seeded entities with all three; the persisted path registered nothing, so a
  limit configured through the API was displayed by the dashboard, returned by
  `GET /admin/users/{id}`, and checked by nothing:

  ```
  after restart, budget: {'budget_limit': None, 'alert_threshold': None}
  alice has spent $500 against a $5 cap -> over_budget=False
  ```

  Worse than a limit that was never set, since the operator has evidence it is
  there. Alert thresholds were lost the same way, removing the earlier warning
  too. Now `_register_persisted_budgets` arms the same three places for loaded
  projects and users as the seed path does, so a limit behaves identically
  whether it arrived from `demo_seed.yaml` or the API. Two deliberate
  asymmetries: a project with no limit is not registered (registering it would
  turn "unlimited" into a policy node carrying no limit), and where a real
  org → team → project hierarchy already has a node for the project, the flat
  fallback node is not written over it — the project's own limit is already
  enforced through `CostTracker`, and replacing a tree node would discard a
  tighter parent cap and *raise* the effective limit.
- **Admin reads reported one task's numbers as the whole fleet's.** Every admin
  aggregate summed `CostTracker._records` — an in-memory list hydrated from
  DynamoDB once at startup and afterwards grown only by requests that instance
  served. Behind the shipped `desired_count=2` that made each answer a function of
  which task the load balancer picked: `GET /admin/overview` alternated
  `total_cost` between `0.000132` and `0` on identical authenticated requests
  after a single chat call. Nothing was ever lost — the records and the budget
  counter were both correct in the table — but an operator would reasonably
  conclude their own client was broken.

  Fixed with two sources rather than one, because only money has a cheap exact
  one. Costs (`current_spend` on `/admin/projects` and `/admin/users`,
  `total_cost` on `/admin/users/{id}`) now read the shared `SPEND#` counter that
  budget enforcement already uses: one `GetItem`, exact, and immune to record
  trimming — a dashboard figure that disagreed with the limit someone was
  throttled on was its own class of confusion. Counts and per-model/user
  breakdowns have no such counter, so they re-read the usage records, rate-limited
  to one refresh per 10 seconds per instance because that read is a paged scan
  whose cost grows with history and the dashboard's traces panel polls every 3
  seconds. Net effect: costs are exact, counts are at most 10 seconds behind, and
  both agree across instances.

  Covers `/admin/overview`, `/admin/usage`, `/admin/usage/export`,
  `/admin/projects`, `/admin/projects/{id}`, `/admin/users`, `/admin/users/{id}`,
  `/admin/models`, `/admin/traces`, `/admin/catalog-drift` and the efficiency
  endpoints — wider than the four originally reported, since `list_users`,
  `list_models`, `traces` and the efficiency analyzer read the same list the same
  way. `request_count` and `total_requests` are now fleet-wide too, which the
  previous release documented as impossible without new storage; re-reading the
  records turned out to be enough, so no new counter was added.

  The refresh deliberately does **not** touch the spend counters, which is the one
  thing separating it from `load_records`: `_bump_spend_fleet_wide` already
  *replaces* each counter with the shared fleet total, so folding the fleet's
  records in on top would count other instances twice and start refusing requests
  under a budget the project had not reached. `/admin/traces` also now sorts by
  timestamp instead of slicing the list tail, because a fleet-wide merge appends
  other instances' records in scan order and the tail stops meaning "recent".
- **The landing page 404s in the container.** The Dockerfile never copied
  `site/`, so `GET /` returned a stub on every containerised deployment while
  working locally — including `docker compose up`, which is the first thing the
  README tells a new user to run. Found by running that instruction against a
  fresh clone rather than by reading the code. The handler is written to degrade
  to a 404 with a pointer to the dashboard (correct for a pip install, where
  `site/` is genuinely absent), which is why nothing logged an error and every
  route test stayed green: they all read from the repo working tree, where the
  directory always exists.
- **`site/infra/` is now excluded from the build context.** The existing
  `infra/` line in `.dockerignore` is anchored to the build root and does not
  match the nested path, so a plain `COPY site/ site/` ships the landing page's
  CDK app into the runtime image. Verified by building it, not assumed. The
  asset handler already refused to serve it — `.py` is not in
  `SITE_ASSET_TYPES` and `infra` is not in `SITE_ASSET_DIRS` — so this is
  defence in depth rather than a disclosure fix.

- **`deploy-fargate.sh` could not run unattended.** The approval mode was
  hardcoded to `broadening`, and CDK needs a terminal to ask, so any CI
  invocation died with `Stack includes security-sensitive updates, but terminal
  (TTY) is not attached` — the script had no way to succeed there at all. It now
  accepts `--yes` and honours `CI=true`. The default is unchanged: an
  interactive run still prompts before widening IAM or network access, because
  having valid AWS credentials is a different question from having agreed to
  those specific grants.

### Added

- **The AWS install skipped creating the project.** Path 3 of the Quick Start
  minted a key scoped to `my-project` and never created it, and nothing failed:
  `/api/chat` answered `200`, spend was recorded, and the only symptoms were the
  project's absence from `GET /admin/projects` and a `null` `budget_limit` — so
  spend accrued uncapped. Found by following the documented steps on a live
  deployment and then noticing the project was not there. Added as Step 6, and
  `axon issue-key` now prints a note when it mints a key for a project that does
  not exist. The ordering itself is deliberate and unchanged: a project id
  scopes a key rather than referencing a record, which is what lets the first
  credential be minted before any project exists.
- `tests/unit/test_container_packaging.py` — asserts the Dockerfile copies every
  directory a request handler reads from, derived from the handler source so a
  new one cannot be silently omitted. This class of defect was invisible to the
  whole existing suite: nothing ran against the built image.

### Repository

- Every GitHub URL now points at `AxonLLM/axonllm`, the canonical public home.
  Seven links in `site/index.html` and `site/architecture.html` pointed at a
  different repository carrying the same description, so "View the source" on
  the public landing page sent visitors somewhere else entirely.

## [0.2.1] - 2026-08-05

Bug fixes only. Everything here was found by running what the documentation said
to run, or by watching a live two-task deployment disagree with itself — not by
reading the code.

One externally visible behaviour change worth noting before upgrading:
`POST /admin/quotas/{project_id}/reset` now answers `503` when it could not clear
the shared spend counter, where it previously always answered `200`. Callers that
treat any non-`200` as fatal will see failures they did not see before; those
failures were previously silent lies.

### Fixed
- **Admin state was per-task, so the answer depended on which instance replied.**
  Found on the live two-task deployment while testing the documented steps:
  `POST /admin/projects` returned `201`, and ten identical authenticated
  `GET /admin/projects` requests then returned the project six times and `[]`
  four times. The project was in DynamoDB the whole time — `AdminRouter.projects`
  is hydrated once at startup and per process, so the task that served the write
  knew about it and the other never would until it restarted. `infra/stack.py`
  ships `desired_count=2` and scales to 10, so this was the default configuration
  rather than an edge case.

  - **Projects now read through to DynamoDB on a local miss** (`_get_project`),
    and the list endpoint merges the stored set with local state
    (`_all_projects`), matching what `APIKeyService` already did for keys — which
    is why authentication never showed this bug while project reads did. The
    merge keeps locally-known objects, because the mutation handlers change the
    resolved object in place and replacing it would discard an in-flight edit.
  - **Key revocations now reach the other instances.** `revoke_key` cleared only
    the cache of the instance that served the request — the one instance needing
    no help — so a revoked key stayed valid elsewhere for up to the 300 s cache
    TTL. `invalidate_cache()` existed for exactly this and nothing called it.
    Revocation now bumps a counter in the table that each instance polls at most
    every 5 s, cutting that window from 300 s to 5 s. A failed poll degrades to
    the old TTL rather than clearing the cache on every request.

  Covered by `tests/unit/test_multi_instance_admin_state.py` (15 tests), each
  verified to fail against the unfixed code. Two of those tests initially passed
  against the bug because the test double returned the same object it stored, so
  revocation propagated through shared object identity in a way DynamoDB never
  would; the double now serializes on write and read.

- **Budget enforcement was per-instance, so a `budget_limit` was per-instance.**
  The second half of the same defect, and the expensive half: usage records all
  reached DynamoDB so reporting was fleet-wide and correct, but `check_budget`
  compared against a counter only the local process had contributed to. With the
  shipped `desired_count=2` a `$100` cap admitted roughly `$200`, and fully
  scaled out roughly `$1000`. Nothing looked wrong from either task — each
  enforced its limit correctly against its own share.

  - **Spend now lives in a DynamoDB counter updated with an atomic `ADD`.**
    Because `ADD` returns the post-update value, the instance recording spend
    learns the fleet total from a write it was already making: no extra read on
    the request path, no cross-instance lock, and no lost update when two tasks
    bill simultaneously. The shared write is deliberately outside the per-project
    lock — holding a lock across a network round trip would cap a project at
    roughly one request per round trip, and `ADD` needs no help being atomic.
  - **`QuotaEnforcer` was the gate that mattered**, not `CostTracker`.
    `CostTracker.check_budget` fills in the `BudgetStatus` on the response;
    `QuotaEnforcer.check_budget` is what returns `allowed=False` and refuses the
    request. Both were per-process and both are now fleet-wide, but only the
    latter was an overspend. They keep separate counter keys because both record
    the same cost on the same request, so sharing one key would double every
    charge and fail a budget at half its limit.
  - **`enforce_all` refreshes a stale figure before deciding.** Adopting the
    total on write is not enough on its own: an instance that has not billed a
    project since starting still held its own `$0` and would admit one request
    per instance against an exhausted budget, again after every deploy. The
    refresh is rate-limited to 2 s, which bounds the overshoot to what the fleet
    can bill in that window rather than eliminating it — the alternative is a
    consistent read on every proxied call.
  - **Startup seeds both counters** from the shared totals, and does so *after*
    `load_records` and by replacing rather than adding: every persisted record
    was already folded into the shared counter when first billed, so summing
    history on top would inflate a project's spend on every restart.
  - **`POST /admin/quotas/{id}/reset` clears the shared counter** and answers
    `503` if it could not, rather than reporting a reset that leaves every other
    instance still blocking the project. `GET /admin/quotas/{id}` reads through
    for the same reason — an operator cannot tell which task answered.
  - **Demo-seed spend stays local** (`share=False`). Every instance fabricates
    the same seed at startup and `ADD` is not idempotent, so sharing it would
    multiply demo figures by the instance count and again on every restart.

  A failed counter update degrades to per-instance enforcement — the previous
  behaviour — rather than to unlimited, and a failed read never reads as `$0`.
  Covered by `tests/unit/test_multi_instance_budget.py` (30 tests) plus route and
  bootstrap coverage; 20 of 21 mutations to the new logic are killed, the survivor
  being an equivalent mutant that the "never move the counter backwards" guard
  makes unobservable.

### Known limitations
- **Rate limits are still per-process.** Both the hierarchy's `rate_limit_rpm`
  and `SlidingWindowRateLimiter` keep their sliding windows in memory, so each
  task admits the configured RPM independently — divide by `desired_count`.
  Unlike spend, a sliding window is not a running total, so it cannot ride along
  on a write the request was already making; sharing it means a read per request.

- **The AWS deploy paths could not be followed as written.** Paths 3 and 4 were
  the two install paths never executed end to end, and all four steps that touch
  the CLI were wrong. Found by running them, not by reading:

  - **`uv pip install -r requirements.txt` fails on a fresh clone** with "No
    virtual environment found" — `infra/` has no `.venv` until something creates
    one. An earlier local check passed only because a previous run had left one
    behind, which is exactly how this survived review.
  - **`cdk bootstrap` is `command not found`.** The CDK CLI is an npm package and
    nothing in the repo installs it globally. `deploy-fargate.sh` had it right
    (`npx cdk`) while the documented command did not — the same shape as the bare
    `axon` bug: the script works, the instruction doesn't.
  - **`--cluster axonllm --service axonllm` and `--task-definition axonllm` all
    resolved to nothing.** The stack never set a physical name, so CDK generated
    `AxonLLMStack-ClusterEB0386A7-rSJKGJp9AqGt` and friends. Verified against a
    live deployment: the cluster returned `MISSING` and `describe-task-definition`
    raised `ClientException`. Steps 3 and 4 — putting provider keys in place and
    checking what the task actually has — were unrunnable, which meant a
    successful deploy still had no way to reach a working gateway.

  The stack now names the cluster, service and task-definition family `axonllm`,
  matching what the docs already told you to type. Four tests pin those names to
  the documented commands, because the failure is invisible from either side
  alone: the stack deploys, the docs read correctly, and they only disagree once
  someone runs step 3.

  The step-1 line also noted as optional, since `deploy-fargate.sh` creates the
  venv and installs requirements itself; only `cdk bootstrap` is genuine
  first-time setup.

## [0.2.0] - 2026-08-05

### Added
- **Read-only admin scopes.** Admin scopes named a resource and nothing else, so
  `admin:quotas` granted `GET /admin/quotas/{project_id}` *and*
  `POST /admin/quotas/{project_id}/reset`. "Let support look at quotas without
  being able to wipe a usage counter" was not expressible — the only choices were
  read+write or nothing. Scopes now take an optional access level:

  | Scope | Grants |
  |-------|--------|
  | `admin:*` | everything |
  | `admin:*:read` | reads on every resource, writes on none |
  | `admin:quotas` | reads **and** writes (unchanged) |
  | `admin:quotas:read` | reads only |
  | `admin:quotas:write` | reads and writes |

  A bare `admin:<resource>` still means both, so **no already-issued key changes
  meaning on deploy** — the suffix narrows and is never required to keep what you
  had. `:write` implies read, because an operator who can reset a quota can
  already see the value being reset and separating them would only produce keys
  that mutate blind. An unrecognised suffix (`admin:quotas:raed`) matches no
  resource and grants nothing, rather than falling back to a resource-wide grant.

  **Read and write are classified by effect, not by HTTP method**, which turned
  out to matter: four admin `POST`s are named like inspections and mutate anyway.
  `quotas/simulate` runs the real enforcer, whose rate-limit check *appends a
  timestamp* and so consumes the project's RPM budget; `regions/health/check`
  updates spoke status and thereby changes where traffic routes; `regions/route`
  exercises the live router; `webhooks/{name}/test` sends a real HTTP request to
  an external host. Classifying by method would have let a nominally read-only
  credential exhaust a rate limit or ping an outside endpoint. `POST
  /admin/pii/preview` is the one non-`GET` that persists nothing, so `:read`
  reaches it.

  The key-issuance guard learned the same vocabulary, so a caller can delegate a
  *narrower* slice of its own authority: `admin:projects` may issue
  `admin:projects:read`, but not `admin:*` and not another resource. Note that
  `admin:projects:read` cannot issue keys at all — issuance is a write to
  `projects`, refused a layer earlier by RBAC.

  Verified on the real app across all 63 admin route/method pairs: `admin:*`
  unchanged at 34 reads + 29 writes, `admin:*:read` at 34 reads and **zero** of 29
  writes. 19 mutations of the new logic: 18 caught, 1 an intended control. Three
  of those initially survived — removing a by-effect override left the path
  classified as a write anyway via the method fallback, so the outcome assertions
  could not tell the two apart; the tests now pin the override itself.

- **A startup guide that answers "what do I do after it's running?"** The four
  install paths were already documented, but three of them ended at a gateway
  with no provider keys, no projects and no way in — and the README's only
  configuration advice was a `.env` file that is silently ignored outside demo
  mode. `README.md` now leads with a decision tree and a path table (auth mode
  and time-to-first-call per path), and each path is numbered steps ending in a
  link to the new **Configuring a clean install** section: where provider keys go
  in precedence order (env var → Secrets Manager → `providers.yaml` → `.env`), the
  per-provider variable names, creating a project, and issuing keys.
  `docs/images/dashboard-clean.png` and `dashboard-seeded.png` show the stat band
  of each so "did the demo data load?" is answerable at a glance rather than by
  reading the flag back.

  The AWS path gained the step that was missing entirely: putting keys into the
  `axonllm/api-keys` secret the stack creates, and forcing a new deployment,
  because secrets are read at container start.

- **Documented authentication and authorization end to end**, including four
  behaviours that were true but written down nowhere:
  - **An API key issued with default scopes cannot reach `/admin/*`.**
    `axon issue-key` defaults to `--scopes chat`, and an API-key context carries
    `roles=["service"]`, which `AdminRBACMiddleware` rejects. So the admin API is
    unreachable after switching to `ENFORCE` unless an `admin:*` key was issued
    first — now called out as ordering, with the verified allow/deny matrix.
  - **A clean install has zero Cedar policies, and zero policies denies
    nothing** — "default deny" is Cedar's rule among the policies that exist, not
    a property of a fresh install, which is the opposite of what a reader assumes
    from the phrase. (As documented here it was `policy_service=None` on an empty
    set; the Cedar fix below replaces that with an always-wired evaluator that
    governs no action, which is the same outcome for a clean install and a
    different one once you add your first policy.)
  - **SCIM group→role resolution does not feed the auth chain.** `roles_for_user`
    is only read on the SCIM path; roles for authorization come from the JWT or
    SAML assertion, so provisioning a user into an admin group grants nothing
    unless the IdP also puts `admin` in the claim.
  - **`aud` and `iss` on a direct OIDC Bearer token are checked only when
    `AXON_OIDC_AUDIENCE` / `AXON_OIDC_ISSUER` are set.** Signature and `exp` are
    unconditional, but an empty audience means a correctly-signed token minted for
    a *different* application is accepted — the confused-deputy shape. The README
    now says to set both rather than treating them as optional tuning.

  Also documented: the four-step credential precedence in `AuthMiddleware` (as a
  diagram), `ENFORCE` vs `LOG_ONLY` per layer, the OIDC claim mapping table and
  the `python-jose` fail-closed behaviour, SAML's five variables and the fact that
  `/saml/acs` returns JSON rather than minting a session, SCIM's fail-closed 503,
  and a minimal production environment block.

- **Tool calling (function calling) pass-through across every provider.**
  `ChatCompletionRequest` now carries `tools` and `tool_choice`, and each adapter
  translates them into its provider's own dialect in both directions:
  - **OpenAI / Azure** — native shape, passed through.
  - **Anthropic** — `input_schema`, `tool_use` / `tool_result` content blocks,
    `stop_reason: "tool_use"` → `finish_reason: "tool_calls"`.
  - **Bedrock Converse** — `toolConfig..toolSpec`, `toolUse` / `toolResult` blocks,
    `stopReason` mapping.
  - **Google AI / Vertex** — `functionDeclarations`, `functionCall` /
    `functionResponse` parts, via the shared `adapters/gemini_tools.py`.
  - **Cohere** — `parameter_definitions` (JSON Schema unrolled into per-parameter
    type/description/required triples) and top-level `tool_results`.
- 60 tests in `tests/unit/adapters/test_tool_translation.py` covering each dialect,
  the tool-loop round trip, the cache key, and request validation.
- Documentation: tool-calling walkthrough in `README.md`, the per-dialect
  translation table and cross-cutting gotchas in
  `docs/internal/axonllm-architecture-and-features.md` §4, and PRD §6.10 (FR-T1…T7)
  with the tool-call request/response shapes in §9.1. PRD OQ7 ("should we support
  tool use?") is resolved — implemented as per-provider translation rather than
  pass-through, since only OpenAI-style providers accept OpenAI's shape.

- **OpenAI Responses API support, for models that reject Chat Completions.**
  The `-pro` tier (`gpt-5.5-pro`, `gpt-5-pro`) is served only by `/v1/responses`;
  on `/v1/chat/completions` OpenAI answers 400 `This is not a chat model and thus
  not supported in the v1/chat/completions endpoint`. The openai adapter now
  detects the tier and switches both the URL and the payload shape: `input` items
  instead of `messages`, `max_output_tokens` instead of `max_tokens`, flat tool
  specs, and tool traffic as top-level `function_call` / `function_call_output`
  items. Sampling parameters are dropped rather than forwarded — these models
  reject `temperature`/`top_p` with a 400 instead of ignoring them, so passing a
  caller's default through would fail every request and trip the circuit breaker.
  Streaming works too: `response.output_item.done` carries the finished
  `function_call` whole, so unlike the hand-built translators this needs no
  cross-chunk accumulation. Restricted to genuine OpenAI — the OpenAI-*compatible*
  providers sharing the same adapter base and URL builder (Azure, xAI, Groq,
  Together, Fireworks, AI21) expose Chat Completions only, and routing a
  `-pro`-looking id there would 404 a request that otherwise worked.

- **Provider keys can come from a `.env` file in demo mode.** `provider_loader`
  reads API keys from `os.environ` and nothing populated it from a file, so a
  `.env` in the project root was never consulted and every direct-API provider
  was silently skipped — while the gateway still answered through Bedrock, which
  authenticates via AWS credentials on a separate path. The failure therefore
  looked like "OpenAI is broken" rather than "no keys were loaded". Reading the
  file is gated on an explicit `AXON_LOAD_DEMO_DATA=true` (the container `CMD`
  does not set it), and an existing environment variable always wins, so
  platform-injected secrets are never shadowed. Startup logs the variable names
  loaded, never their values.

- **Production readiness checklist on `/admin/production-checklist`, with a live
  provider model-availability check.** Seven checks, each covering a state the
  gateway serves traffic in without complaint — the config is wrong in a way no
  request surfaces:
  - **Pinned model ids still exist at their providers** (new). `config/models.yaml`
    pins a provider-side `model_id`; providers retire, rename and alias those on
    their own schedule, and nothing reconciled the two. The dangerous case is not
    the 404 — that fails over noisily — but the *undocumented alias*: `xai`'s
    `grok-3` answered 200 while resolving to `grok-4.3`, and because an alias
    appears on no price list it also billed **$0.00** while being served by a
    $1.25/$2.50-per-MTok model. The check asks each provider what it currently
    serves and diffs that against the routing table. Verified against the live
    APIs: it flags `grok-3` and a retired OpenAI snapshot, and correctly does
    *not* flag `claude-opus-4-1-20250805`, which Anthropic still lists.
  - **Token pricing covers every provider mapping** — reuses `audit_pricing`, so
    it cannot disagree with the pricing page. FAIL when a model has no priced
    provider at all (bills $0.00 outright, so a budget cap on it can never
    trigger); WARN when partially priced, since the priced provider still bills.
  - **Every routed provider has credentials.** `load_provider_configs` drops
    providers with no key without raising, so a missing credential is invisible
    until a request routes there. FAIL when a model has no usable provider at
    all. Bedrock and Bedrock-Mantle count as credentialled — they authenticate
    through the boto3 chain, not `providers.yaml`, so treating their absence as
    missing would fail this check for most deployments.
  - **API authentication is enforced.** `AXON_AUTH_MODE=LOG_ONLY` serves every
    unauthenticated request while logging that it would have denied it, which
    makes the audit trail read like the control is working.
  - **Demo seed data is not loaded** — WARN when `AXON_LOAD_DEMO_DATA` is merely
    *unset* rather than explicitly false, because `serve_dashboard.py` (the
    container `CMD`) defaults it to `true`, so the same image started the
    ordinary way seeds fabricated spend into a real dashboard.
  - **State survives a restart.** DynamoDB writes are swallowed by design — a
    provider call should not 500 because Dynamo hiccuped — so an unreachable
    table loses every billing record with no request-visible symptom.
  - **Issued API keys are scoped and expire.** WARN, not FAIL, because neither
    half is something the operator can fix by flipping a setting. *Scopes are not
    enforced on the data plane*: `AuthMiddleware` puts them on the request
    context and `admin_rbac` reads them for `/admin/*`, but nothing consults them
    on `/v1/*` — a key issued `["models:read"]`, and a key issued `[]`, both call
    `/v1/chat/completions` and spend money, so a scope string reads like a
    capability boundary while being documentation. What actually constrains a key
    is its project's `allowed_models` and budget. *And `expires_at` defaults to
    none*: there is no maximum age and no rotation reminder, and `rotate_key`
    carries the old expiry through, so rotating a non-expiring key yields another
    one and revocation is the only thing that reliably stops a key.

  Three properties the checklist is built around. **A check that could not run
  reports UNKNOWN, never PASS** — collapsing those is how an expired credential
  or a blocked egress route renders as a green checklist, so disabling the live
  check leaves the row unknown rather than passing it. **Nothing is enforced**: no
  check can refuse a boot or reject a request, and the page says so, because a
  readiness page that can take down a deployment is one nobody enables. **List
  calls only, never a completion** — listing is free and idempotent, while
  probing an id by generating a token would make loading an admin page a billable
  event; the cost is that alias detection has to be inferred from absence rather
  than observed, and the page reports that ambiguity instead of guessing.

  The availability check spans three authentication styles: bearer-token HTTP
  endpoints, **Bedrock** via boto3, and **Bedrock Mantle** via a SigV4-signed
  `GET /v1/models` — Mantle is OpenAI-shaped but signs with SigV4, so it cannot
  use the bearer-token path. Bedrock reads **two** catalogues, and the second is
  not optional: `models.yaml` pins cross-region inference profiles
  (`us.anthropic.claude-opus-4-6-v1`) for 10 of its 14 mappings, and
  `list_foundation_models` does not return those, so checking it alone would
  report the majority of working Bedrock mappings as retired — a wall of
  confident false findings about the provider carrying the most traffic. A caller
  holding `ListFoundationModels` but not `ListInferenceProfiles` is treated as a
  failed read rather than a short catalogue, for the same reason. This takes
  live coverage from 18 mappings to **43**, leaving nothing in the current
  routing table unchecked; Vertex AI and Azure OpenAI remain unchecked by design,
  since their ids are deployment paths where listing proves nothing.

  Production only. In demo mode the page explains why it is empty rather than
  rendering checks that would all fail correctly and mean nothing, and makes no
  outbound calls at all. Anything unchecked is counted *by name*, so a partial
  check never implies full coverage. A failed provider call yields an error row
  and no findings — a network blip cannot manufacture a wall of bogus "retired
  model" warnings — and an empty model list is treated as a changed response
  shape rather than as "everything is missing". Credentials never reach a
  rendered string: transport failures report the exception type only (httpx
  embeds the request URL, which for Google AI carries the key as a query
  parameter; a botocore error can carry the caller's ARN) and the Google AI key
  is passed via `params` rather than interpolated into the URL. 91 tests in
  `tests/unit/test_production_checklist.py` and
  `tests/unit/test_model_availability.py`.

- **Pricing coverage report, at startup and on `/admin/pricing-drift`.**
  `config/models.yaml` and `config/pricing.yaml` are edited independently and
  nothing checked one against the other, so a model added without a price failed
  twice in silence: `CostTracker.calculate_cost` returns 0.0 for an unknown
  provider/model pair — so the usage record carries no cost, project spend does
  not move, and budget blocks and quota alerts under-count rather than erring
  safe — while smart routing, having no rate to read, scores the model on the
  average of the known prices. Neither path raises, which is why this is a page
  rather than a log line: when the page was written **33 of 48 provider mappings
  had no price at all**, across seven providers with no section in the file (see
  the entry below for what filling those in found). The page
  lists every unpriced mapping, every pricing entry no model reads (usually the
  other half of a renamed model id), and a paste-ready YAML fragment for the
  gaps — with rates left at `0.0`, since a guessed price bills silently where a
  missing one is visible. Rename hints match on the model *family* rather than
  string similarity: `mistral-large-2402` → `-2407` is a version bump worth
  reusing a rate for, but `claude-haiku-4-5` → `claude-opus-4` is a different
  tier at roughly 15× the price, and suggesting it would overcharge and look
  deliberate. The audit performs the same provider + provider-side-`model_id`
  lookup the biller does, so a mapping it calls priced is exactly one
  `CostTracker` can find. Gated on unpriced mappings only, so the banner clears
  once every model has a rate. The dev server also opens the page when it finds a
  gap, but only on an interactive terminal — `serve_dashboard.py` is the
  container `CMD`, so a piped or containerized run prints the banner and nothing
  else; `AXON_NO_BROWSER=true` suppresses it locally too.

- **Real published rates for 20 of the 33 unpriced mappings** — coverage 31% →
  73%, and five providers that had no section in `config/pricing.yaml` at all
  (`ai21`, `bedrock-mantle`, `google_ai`, `groq`, plus new entries under
  `openai`, `anthropic` and `bedrock`) now bill at a real rate instead of $0.00.
  Every figure is from the provider's own price list, cited by URL and fetch date
  in the file header: OpenAI, Anthropic and Google AI pricing pages, `groq.com`,
  `ai21.com`, and the AWS Price List API for Bedrock and Bedrock-Mantle.
  Three judgement calls are documented inline rather than buried:
  - **Regional vs global Bedrock endpoints.** Claude 4.5 and later carry a 10%
    premium on regional endpoints, and the `us.` prefix in `models.yaml` *is* a
    regional inference profile — so those entries take the regional rate
    ($1.10/$5.50 per MTok for Haiku 4.5, not the $1.00/$5.00 global figure).
  - **Mantle SKUs are priced from the Bedrock rate.** Of the 66 model/direction
    pairs where us-east-1 lists both a `…-mantle-…-standard` SKU and a plain
    on-demand one, all 66 agree to the cent — checked rather than assumed, since
    that equivalence is what licenses six of the new entries.
  - **Sub-threshold context tiers.** Where a provider charges more above a
    context length (`gemini-3.1-pro-preview` doubles above 200k, `gpt-5.5-pro`
    above 272k), the lower rate is used and the premium noted. `CostTracker`
    prices a request from token counts alone and has no context tier to switch
    on, so encoding the higher rate would overcharge every ordinary request.

  13 mappings were left unpriced at this point on the reasoning that their
  providers publish no rate for the id being sent. Checking each against the
  provider's live API rather than its pricing page showed that reasoning was
  wrong for five of them — see the entry below.

- **Stale and unroutable provider model ids, found by probing the live APIs.**
  A pricing page is a marketing document and a model list is not a guarantee of
  capacity, so every id `models.yaml` and the adapter `_MODELS` lists advertise
  was tested with a real completion. Coverage rose 73% → 84% as a result, but the
  more useful finding is that "absent from the price list" turned out to predict
  almost nothing about whether an id works:
  - **`grok-3` and `grok-3-mini` answered 200 — and xAI resolves both to
    `grok-4.3`**, which it reports in the response's `model` field. Undocumented
    aliases: absent from `/v1/language-models` and from the price list, so
    unpriceable, so every Grok request billed **$0.00 while being served by a
    model that costs $1.25/$2.50 per MTok**. Nothing failed, which is why it
    survived — a 404 would have been caught the first time anyone tried it. Both
    are replaced by the real tiers, `grok-4.3` and `grok-4.5`, now priced from
    xAI's own API (which reports in hundred-thousandths of a cent per token —
    calibrated against the published page rather than assumed).
  - **`grok-2-vision-1212`** was advertised in `xai_adapter._MODELS` and is
    simply gone: 400 `Model not found`.
  - **Four of Together's five advertised ids need a dedicated endpoint.**
    `DeepSeek-R1`, `Qwen2.5-72B-Instruct-Turbo`, `Llama-4-Maverick-FP8` and
    `Mistral-Small-24B` all return 400 `Unable to access non-serverless model`.
    That is a provisioning error, not a wrong id, so no rename fixes it — and it
    is not fixable by picking a newer snapshot either: R1-0528, V3.1, Qwen2-72B,
    Qwen3-Next-80B, Qwen3.5-397B and QwQ-32B are all equally unavailable. The
    Qwen3.x `-Plus`/`-Max` tier *is* serverless but **streaming-only** (400 `This
    model only supports streaming`), which this gateway's non-streaming path
    cannot use. Both files now list only ids confirmed by a live non-streaming
    completion: Llama-3.3-70B-Turbo, DeepSeek-V4-Pro, Qwen3.5-9B and
    gpt-oss-120b, all priced from Together's own API.
  - **`gemini-3-pro-preview` is still served** — listed by `/v1beta/models` with
    `generateContent` support — despite being off the Gemini price list. So it is
    a working model with no published rate, the one case neither file can fix: the
    id is right and there is no number to put. Kept and left unpriced; the earlier
    claim that 3.1 Pro Preview replaced it was wrong.

  **6 mappings remain unpriced, all for reasons that survive checking:** the five
  `bedrock-mantle` `openai.gpt-5.x` ids (`gpt-5.4`, `-5.6-luna` and `-5.6-terra`
  are published only in `us-gov-east-1`/`us-gov-west-1` at $3.30/$19.80 per MTok;
  `gpt-5.5` and `-5.6-sol` appear under none of the four Bedrock service codes)
  and `gemini-3-pro-preview` above. They are kept rather than removed: "no
  published rate" is not evidence an id is wrong, and deleting a working model is
  worse than leaving one on the drift report.

  The two `fireworks` ids **are** priced, contrary to an earlier note here that
  said Fireworks published no per-model table and left them unpriced on that
  basis. That was wrong: the aggregate `/pricing` page shows tiers only for
  embeddings, but each serverless text model carries its own input/cached/output
  rate on its own `fireworks.ai/models` page, which is where the rates in
  `config/pricing.yaml` come from.

- **Semantic caching, so a reworded question can be served its earlier answer.**
  It runs only after the exact key has missed, so the cheap path stays cheap, and
  it is off unless *both* the gateway (`AXON_SEMANTIC_CACHE`) and the project opt
  in — a project flag alone cannot turn on a feature that calls Bedrock Titan for
  an embedding on every miss. Three things make a false hit unlikely enough to
  ship: a **0.90 cosine threshold**, chosen for its distance from the
  highest-scoring *different*-question pair on the calibration set (0.7476) rather
  than for its hit rate; a **literal-token guard**, so two prompts that differ on
  a number, an identifier or a quoted string never match however close the vectors
  are; and **skipping the cache entirely** for streaming, tool-carrying and
  non-zero-temperature requests, where a reused answer is wrong by construction.
  `GET`/`DELETE /admin/semantic-cache` report and clear it.

- **The literal guard compares polar words by axis, not by set difference.** It
  used to block whenever two prompts contained *different* polar words, which
  conflated "disable" vs "enable" (different questions — block, correct) with "on"
  vs "enable" (one question, two phrasings — block, wrong). The second is the
  common case, since a paraphrase almost never reuses the same polar vocabulary,
  so the guard rejected most of the hits the embedding existed to find. It was
  also unsafe in the other direction: with only uninflected forms listed and no
  notion of which word opposes which, "which types are included" and "which types
  are excluded" passed as agreeing. `_POLAR_AXES` now groups opposites into seven
  axes and blocks only across the same axis, with `_ONE_SIDED_BLOCKS` naming the
  axes where a lone word decides on its own. On a 45-pair labelled corpus,
  must-block went 24/26 → 26/26 and must-allow 6/19 → 17/19.

- **Optional entity detection for PII a regex cannot match.** `PII_PATTERNS` has
  no name pattern, because a name has no shape — which is why a name survived
  redaction while an SSN did not. AWS Comprehend now supplies name, address and
  age detection when `pii_ner_enabled` is set. It is off by default and priced
  per call (~$0.0001 per 100 characters), and it **fails open**: if boto3 is
  absent or the call errors, the regex redactions still apply rather than the
  request failing. `POST /admin/pii/preview` shows the before/after directly,
  which is the only way to see redaction without the model's own refusal
  behaviour confounding the result.

- **Catalogue drift report on `/admin/catalog-drift`.** `models.yaml` decides what
  the router can dispatch to; `catalog.yaml` describes what those models *are*.
  The two are edited independently, nothing checked them against each other, and
  neither is wrong on its own terms — so the drift is invisible. Three
  consequences, none of which raises anything: the catalogue answers for models no
  mapping can reach (offered in the picker, then unselectable); routed mappings
  with no metadata return `capabilities: []`, so "does this model do vision" is
  answered *no* rather than *unknown*; and a usage record can name a model
  `models.yaml` never declared, since records carry the resolved provider. That
  third case is why this is a page and not a lint rule — only the join against
  recorded usage catches a model being called but never declared, and only usage
  distinguishes "populate metadata for 46 models" from "for the 9 carrying
  traffic".

### Fixed
- **A conduct report was directed to a public issue.** `CODE_OF_CONDUCT.md` asked
  for harassment to be "reported to the project maintainers privately, by opening
  a GitHub issue marked for maintainer attention" — but issues are public,
  including to the person being reported, so following the instruction exposed the
  reporter in the act of asking for privacy. Both this file and `SECURITY.md` now
  point at GitHub's private vulnerability reporting form, the only channel on the
  repository that is private to maintainers by construction. Neither file names an
  email address: the previous `security@axonllm.dev` and `conduct@axonllm.dev` sat
  on a domain with no DNS record, so reports bounced silently and the sender
  believed they had disclosed.
- **Privilege escalation: a narrow admin scope could mint or steal a full admin
  credential.** `AdminRBACMiddleware` authorizes on the first path segment, which
  is the right granularity for most of the admin API and the wrong granularity for
  the routes that hand out credentials. Two paths, both confirmed against the real
  app rather than reasoned about:

  - `admin:projects` reaches `POST /admin/projects/{id}/keys`, which passed the
    request's `scopes` straight through to `APIKeyService.issue_key`. Asking for
    `scopes=['admin:*']` returned `201` with a working superadmin key.
  - `admin:keys` reaches `POST /admin/keys/{key_id}/rotate`. `rotate_key` copies
    the *old* key's scopes onto the replacement and the handler returns the
    replacement's raw value — so rotating a colleague's `admin:*` key handed over
    admin access, revoked the victim's key as a side effect, and needed no
    cooperation and no second project.

  Also fixed alongside them: a project-scoped key could list, issue, revoke and
  rotate keys in *other* projects, with `GET /admin/projects/{other}/keys`
  returning their key ids and metadata.

  The checks live in the handlers, not the middleware, because the middleware sees
  only a path — whereas the decision needs the scopes the *body* asked for and the
  project the *target key* belongs to. The rules: grant only admin scopes you
  already hold, rotate only keys whose admin scopes you already hold, and stay
  inside your own `project_id`. `admin:*` and the `admin` role are unrestricted;
  non-admin scopes stay freely grantable, since the constraint is on escalating
  admin authority rather than on delegating ordinary access. `LOG_ONLY` logs
  instead of enforcing, because that mode exists to issue the first key before any
  credential exists.

  Ordering mattered in one place: `rotate_key` revokes before re-issuing, so a
  check placed after the call would refuse the response having already destroyed
  the credential — turning a blocked escalation into a denial of service. There is
  a test for that specifically.

  Found by sweeping all 63 admin routes with four real issued keys instead of
  trusting `test_admin_rbac.py`, which passed throughout: it wires a fake auth
  middleware around three mock routes, so it never exercised the real route table.
  The new tests drive the real routes behind the real middleware pair. 17
  mutations of the new guards: 15 caught, 1 an intended control, 1 a real gap (the
  constructor's default mode was unpinned — a fail-open default nothing asserted),
  now covered.

  The same sweep found `POST /admin/policies/hierarchy` returning **500 on a
  malformed body**: it read `body["node_id"]` / `body["node_type"]` directly and
  caught only the `ValueError` from `set_node`, so a missing field raised
  `KeyError`. Every sibling admin `POST` answers 400 for the same input; this one
  now does too.
- **The documented quickstart could break the host Python.** The README opened
  with `pip install -e ".[dev]"` and no virtualenv step, so outside an activated
  venv it installed into whatever Python was on `PATH` — and because the extras
  declare floors rather than pins, it resolved *down*: `httpx>=0.25.0` yielded
  httpx 0.25.2, old enough to break unrelated packages sharing that environment.
  The repo already committed a `uv.lock` and a uv-created `.venv`; nothing
  mentioned it. Every install path is now `uv`, which cannot make this mistake,
  and CI additionally runs `uv lock --check` so the lockfile cannot silently drift
  from `pyproject.toml`. Three latent problems surfaced while testing the paths
  end to end: `deploy-fargate.sh` only sourced the venv in its `else` branch, so
  the *first* run of a fresh checkout deployed with it inactive; the Dockerfile
  installed six hand-listed unpinned packages that had drifted from
  `requirements.txt` (which itself omitted `uvicorn`); and `python-jose` was
  imported by the OIDC path but declared in no extra, so JWT verification
  fail-closed on a complete install — it now has an `oidc` extra.

- **The exact-match response cache never wrote, so every lookup was a miss.**
  `cache_manager.put` had no call sites at all: the pipeline read the cache at
  step 9 and nothing ever populated it, which meant the per-project
  `cache_enabled` flag bought precisely nothing while appearing to be on. Step
  15.5 now writes, and its placement is deliberate — *after* response guardrails
  and PII re-injection, so a later hit cannot bypass either, and after the
  streaming return, so only complete responses are stored.

- **Two models routing round-robin advanced the same counter.** `Router`
  instantiates one `RoundRobinStrategy` and reuses it across every model, but the
  strategy tracked a single global `_index` — so each model skipped providers
  instead of cycling its own mappings in order. With two 3-provider models
  interleaving, each saw 0, 2, 1, 0 rather than 0, 1, 2. The cursor is now keyed
  per provider set, including `model_id` so two models fronted by the same
  providers stay independent, and sorted so config ordering cannot fork it.
  Single-model deployments were unaffected.

- **Provider response ids collapsed traces and under-reported spend.** The agent
  generated a unique `request_id`, then overwrote it with `response.id` before
  building the `UsageRecord` — and provider ids are not guaranteed unique per
  call: all three Bedrock Mantle routes fall back to a constant
  `"mantle-response"` when the upstream response carries no id. Two things key off
  that value. Trace and span ids hash from it, so every affected Mantle request
  produced an identical `trace_id`/`span_id` and a backend keyed on those saw one
  span retransmitted rather than N calls — which is why Mantle traffic went
  missing from traces. And `cost_tracker.load_records` de-dupes by it, so on
  restart-rehydration five persisted records became one. The DynamoDB PK is
  `USAGE#{request_id}`, so those rows also shared a partition key, separated only
  by the timestamp SK. The gateway's own id is now kept and the provider's carried
  alongside as `provider_request_id` — persisted, exported on the trace event, and
  set as an OTLP attribute only when the provider supplied one. The audit trail
  already used the gateway id, so audit and usage rows now agree for a given
  request instead of disagreeing. The client-facing response `id` is unchanged: it
  still returns the provider's, so the API contract holds.

- **Six admin write endpoints returned success and lost the change on the next
  restart.** Each one mutated a shared in-memory object and never wrote to
  DynamoDB. Because `GET` reads back that same object, the endpoint looked correct
  for the life of the process — the write was gone only after a deploy, with
  nothing in between to suggest it had failed:
  - **`POST`/`DELETE /admin/projects/{id}/members`** — a missed call site, not a
    missing feature: the two sibling handlers on the same object
    (`add_project_model`/`remove_project_model`) persisted correctly, so
    membership changes appeared to stick whenever a model edit followed them in
    the same process and reverted whenever one didn't. Revocation is the direction
    that matters — a member an operator had removed from a project regained access
    at the next deploy. The duplicated persist block is now one `_persist_project`
    helper shared by all four handlers, because the omission *is* the bug.
  - **`POST`/`DELETE /admin/webhooks`** — event destinations had no persistence
    support at all. Adding one produced an endpoint that reported success and then
    silently stopped delivering security events at the next deploy; the failure
    mode is an *absence* of alerts, which nothing observable distinguishes from
    "no events occurred". `POST` also appended rather than replaced on a repeated
    name, so a re-POST double-delivered every event to that destination and
    remove-by-name then deleted only one of the pair; it now replaces and reports
    `200 updated` vs `201 created`.
  - **`POST`/`PUT`/`DELETE /admin/regions/spokes` and `PUT /admin/regions/config`**
    — likewise unpersisted, so a region an operator had drained came back into
    rotation on restart, and hub-level failover timings reverted to `spokes.yaml`.

  New `event_destination` and `region_topology` entity types in `persistence.py`,
  both stored as a **single item holding the whole set** rather than a row each —
  verified necessary, not stylistic. With a row per destination, a *deletion* is
  unrepresentable while demo seeding is on: the delete removes the row, the next
  boot re-seeds the destination, and no row remains to record that an operator
  removed it. A restart probe caught exactly that (a deleted `legacy-pagerduty`
  came back and resumed receiving events) after the first cut of this fix, which
  is why the startup load **replaces** the seeded set rather than merging by name.
  An empty stored set means "everything was removed" and is honoured as such —
  hence `load_event_destinations`/`load_region_topology` return `| None` rather
  than a bare collection, and bootstrap tests `is not None`, not truthiness.

  Spoke `status` and `PUT /admin/regions/{region}/status` are deliberately *not*
  persisted: that is health-check state, and restoring a stale `unhealthy` would
  hold a recovered region out of rotation while a stale `healthy` would route to a
  region that is still down. Spokes come back at their default and the first probe
  decides.

  41 tests in the new `test_admin_writes_persistence.py`, driving the real routes
  through the real serializers with only the boto3 boundary replaced, and
  asserting the deletion direction for all three resources — an add can persist
  while a delete silently reverts. Mutation testing reverted 21 individual pieces
  of the fix; 18 were caught, and the 3 survivors are equivalent mutants (a stored
  `"[]"` is a truthy string; rebinding `hub_config.spokes` is as visible as
  mutating it, since the router and monitor hold the object). Write failures stay
  logged-and-swallowed as elsewhere in the admin API — the in-memory change
  already happened — with `last_write_error` surfacing the drop to a health probe.
  README documents the replace-not-merge semantics, the deliberate
  non-persistence of health state, and the four spoke routes that were missing
  from the endpoint table entirely.

- **The Cedar authorization layer was unusable in every direction at once:
  adding your first policy took the gateway offline, and no policy you wrote ever
  persisted or took effect.** Four defects compounding:
  - **The first `permit` bricked everything it didn't match.** Evaluation was
    default-deny globally, so `permit(principal, action == Action::"read",
    resource);` — the obvious first policy, and the one the README suggested —
    403'd all eight write endpoints including `POST /api/chat` and
    `POST /admin/policies`. That last one meant the gateway could not be recovered
    through its own API. Deny is now scoped per action: an action is governed once
    some `ENFORCE` statement names it (or omits the action clause), and within a
    governed action Cedar's rules are unchanged — a permit is required, and forbid
    still wins. An action nobody wrote a rule about falls through to
    authentication, admin RBAC, and quota enforcement, which are always on.
  - **A single `LOG_ONLY` policy denied every method.** `LOG_ONLY` statements
    `continue`d without setting `permitted`, so under global default-deny the
    documented way to *safely trial* a policy — and the mode `POST /admin/policies`
    defaults to — was the worst case: GET, POST, PUT, PATCH, DELETE and HEAD all
    DENY. LOG_ONLY now governs nothing, so it cannot change a decision.
  - **Policies never persisted and never took effect.** `create_policy` had no
    persistence call and statements were compiled once at construction, so a
    policy applied only after a restart it could not survive. Added
    `save_cedar_policy` / `load_all_cedar_policies` to `persistence.py` (keyed
    `CEDAR_POLICY#<name>`, matching the route's update-by-name identity),
    `CedarPolicyService.reload()`, and the wiring: bootstrap builds one evaluator
    and hands the same instance to both `AdminAPI` and `AuthMiddleware`, so a
    `POST` recompiles the object requests actually consult. Persisted policies
    load at startup and merge over seeded ones by name.
  - **Scope clauses the parser can't honour were silently dropped, widening the
    statement.** `resource == Resource::"/api/chat"` parsed and then forbade
    *every* write; `principal == User::"alice"` parsed and then permitted
    everyone. Both narrow a policy, so ignoring them fails open. These, plus
    `principal in`, `resource in`, `action in [...]`, and a bare `permit;` with no
    scope triple (which parsed as "permit everything"), are now rejected — with a
    400 from `POST /admin/policies` rather than a log line at startup nobody
    reads. The module docstring had claimed `resource == Model::"gpt-4o"` was
    supported; it never was.

  65 tests across `test_cedar_policy.py`, the new `test_cedar_policy_lifecycle.py`
  (the route → evaluator → persistence path end to end), `test_bootstrap.py` (the
  shared-instance wiring and startup load), `test_admin_routes.py`, and
  `test_e2e_starlette.py` (a read-only policy no longer 403s `/api/chat`). Three
  pre-existing tests asserted the old behaviour and now assert the new semantics
  rather than being deleted. README gained a
  [Cedar authorization policies](README.md#cedar-authorization-policies) section:
  the supported/unsupported clause table, how a decision is reached, why step 4
  departs from textbook Cedar, that a `permit` grants nothing you didn't already
  have, and a warning that an `ENFORCE` `forbid` on `write` can still lock you out
  of the policy API — with both verified recovery routes.

- **A Fargate deploy came up seeded with fictional tenants.** `infra/stack.py`
  set five environment variables on the task definition and not
  `AXON_LOAD_DEMO_DATA` — but absent is not neutral for that variable, because
  the container `CMD` is `serve_dashboard.py`, which defaults it to `true`. Every
  enterprise install therefore started with Acme Corp, three fictional users and
  66 fabricated usage records, merged into the same DynamoDB table as real state
  and indistinguishable from it in the UI. Worse than a visible failure: the
  gateway looked like it was already carrying traffic. The stack now sets
  `"false"` explicitly, and `tests/unit/test_infra_stack_env.py` asserts it —
  along with `AXON_AUTH_MODE=ENFORCE`, which has the same property (dropping it
  would open the deployment rather than fall back to the safe value). Turning it
  back on for a deployed demo is now the deliberate edit; README path 4 covers
  it. Note that this stops re-seeding, not deletion: rows a previous run
  persisted survive the flag.

- **Four `UsageRecord` fields never survived a DynamoDB round trip, and one of
  them came back claiming success.** `serialize_usage_record` wrote 14 of the
  dataclass's 18 fields; `cache_creation_tokens`, `latency_ms`, `status` and
  `routing_strategy` were silently absent. Every record restored from DynamoDB
  therefore carried the dataclass default for all four rather than what actually
  happened, which is the same shape as the `dominant_task_type` bug below: the
  field reads as present, nothing raises, and the value is a constant no request
  produced.

  `status` is the one that mattered, because its default is `"success"`. A table
  containing failed requests restored as a set asserting every request had
  succeeded — any error rate computed over restored records read 0%. That is
  worse than missing data: it is confidently wrong in the direction that looks
  healthy. `cache_creation_tokens` is the same story one layer down, since cache
  creation is billed at its own rate and a restored record priced it as ordinary
  prompt tokens.

  All four are now written and read back. The deserializer defaults an *absent*
  field to `""` / `0` rather than to the dataclass default, so a row written
  before this change reports "unknown" instead of asserting a plausible
  measurement — the same rule `task_type` already followed, where `""` (not
  classified) must never be read as `"general"` (classified as general).
  `latency_ms` is cast with an explicit `float()` because
  `_convert_decimals_to_native` narrows a whole-valued `Decimal` to `int`, so a
  latency landing exactly on `1234.0` would otherwise come back typed `int` for
  that row alone.

  The regression test that matters here is derived from
  `UsageRecord.__dataclass_fields__` rather than a hand-written list of names:
  the next field added to the dataclass now fails until it round-trips. Nothing
  connected the field list to the serializer before, which is precisely how four
  additions drifted out without anyone noticing.
- **Every user's `dominant_task_type` reported `general`, because the field was a
  hardcoded literal.** `UserEfficiencyProfile.dominant_task_type` was the string
  `"general"`, never derived from anything: a user who sent nothing but math
  questions looked identical to one who sent nothing but code. The literal was
  only the visible half. `UsageRecord` had no `task_type` field at all, so the
  classification — which *did* run correctly per request, and did drive routing —
  was discarded immediately after use. By profile-build time there was nothing
  left to aggregate, and the constant was standing in for data that had never
  been persisted. The tell was that the *empty*-records path returned `"unknown"`
  while the populated path returned `"general"`: a user with 500 requests looked
  *less* classified than a user with none.

  `UsageRecord` now carries `task_type`, stamped at both construction sites (the
  blocking path and the streaming path's end-of-stream accounting) from the smart
  routing decision when there is one and from the classifier otherwise — the
  fallback matters because most requests never go through smart routing, and
  without it the aggregate would describe only the auto-select minority while
  appearing to cover everyone. Classification is wrapped so it can never fail a
  request that has already been served.

  Throughout, `""` (not classified) is kept distinct from `"general"` (classified
  as general). Rows written before the field existed deserialize to `""`, and the
  mode counts only classified records, falling back to `"unknown"`. Collapsing the
  two would recreate the original bug for historical data — the population most
  likely to be affected by it — which is why it is pinned by tests on both the
  persistence round trip and the aggregate.
- **`mantle_adapter`'s advertised model list, four of six ids of which were not
  served.** Found by pointing the new availability check at Mantle.
  `anthropic.claude-sonnet-4-6`, `anthropic.claude-opus-4-6-v1` and
  `anthropic.claude-haiku-4-5-20251001-v1:0` carried Bedrock-style version
  suffixes Mantle does not use, and `meta.llama4-maverick-17b-instruct-v1:0` has
  no `meta.*` equivalent in the Mantle catalogue at all. Nothing broke, because
  request routing reads `config/models.yaml` rather than any adapter's `_MODELS`
  — which is precisely why the drift went unnoticed: it is an advertising surface
  no request exercises. Replaced with the 11 ids the registry actually routes to,
  all verified served, and pinned in both directions by `TestAdvertisedModels` so
  the list and the routing table cannot diverge again without a test failing.
- **Claude Opus 4.8 was billed at 3× the published rate.** The `anthropic`
  entry read `0.015/0.075` per 1K tokens — the retired Opus 4.1 rate — while
  Anthropic prices Opus 4.5 and later at $5/$25 per MTok, a third of the earlier
  generation. So every Opus 4.8 request overcharged: a 1M-in/100K-out call
  recorded $22.50 against a real cost of $7.50. This is the *opposite* failure to
  an unpriced model and invisible for the same reason — nothing cross-checks a
  rate that parses, and the drift page can only report a price that is missing,
  not one that is wrong. Budget blocks and quota alerts fired early against
  inflated spend, which reads as a working system rather than a broken one.
- **Smart routing ignored cost entirely — `cost_quality_tradeoff` was inert.**
  `_get_model_cost` read pricing off the model registry, which is populated only
  from an inline `pricing:` block in `models.yaml` — and there are none (0 of 48
  provider entries). `config/pricing.yaml` was loaded, but only into
  `CostTracker`, never into routing. So every one of the 45 models costed 0.0,
  the cost term collapsed to `tradeoff × (1 − 0)` — a constant added to every
  candidate — and ranking became pure benchmark order. The documented 70/30
  quality-vs-cost blend silently always picked the highest-benchmark model,
  which is generally the most expensive one: the opposite of the configured
  intent, with no error to notice it by. Routing now resolves pricing from the
  same provider/model_id table `CostTracker` bills from, so the cost used to
  choose a model and the cost actually charged cannot disagree.
- A missing price is now treated as **unknown rather than free**. With most
  provider entries unpriced at the time, scoring absent pricing as 0.0 would make an
  unpriced model the cheapest possible candidate — it would win for being
  *unmeasured*, not for being cheap. On the `general` panel that hands selection
  to `claude-haiku` (benchmark 78) over `claude-sonnet` (90). Unpriced
  candidates are scored at the mean of the known costs, excluded from the
  normalization maximum, and flagged `cost_estimated` in the decision trace.
- **`gpt-5.5-pro` was configured to route over Chat Completions**, where it can
  only ever 400 — the model was unusable from the day it was added. Fixed by
  implementing the Responses API path above rather than deleting the config entry,
  since `-pro` is a class of models and the next one added would fail the same way.
- **Tool specs were silently dropped.** `ChatCompletionRequest` had no `tools`
  field, so `_parse_request` never read one: the request succeeded, the model was
  simply never told any tools existed, and it answered confidently that it had no
  such capability. A fluent HTTP 200 with the entire tool-use loop missing, and no
  error anywhere to notice it by.
- The PII path rebuilt the request field-by-field, so any field added later was
  dropped — which is exactly how `tools` went missing. It now uses
  `dataclasses.replace`, so a caller's tools survive a request that happens to
  contain PII (an intermittent failure far harder to find than a total one).
- Smart routing read the *last* message as "the prompt", so mid-tool-loop rounds
  classified a tool result and could be sent to a different model than the round
  that chose the tool. It now walks back to the last real user text.
- The semantic cache key omitted `tools`/`tool_choice`: the same prompt sent with
  a tool list can return a tool call and sent without one returns prose, so a
  cached tool-free reply could be served to a request that needed a tool call.
- Request validation rejected an assistant turn with `tool_calls` and no
  `content`, which is the normal shape of a tool call — it broke every tool loop
  at round two.
- Gemini rejects unknown JSON Schema keys (`additionalProperties`, `$schema`,
  `title`, `default`) rather than ignoring them, so a schema written for OpenAI
  400'd the whole request. Schemas are now filtered recursively.
- **Bedrock Mantle dropped tools on all three of its routes.** It is the one
  provider that bypasses the adapter layer — it hand-builds a payload per Mantle
  API — so the translation above never reached it, and 11 models advertised
  `function_calling` while their tool specs went nowhere. Each route now
  translates in both directions, which is three dialects rather than one:
  `/anthropic/v1/messages` takes `input_schema` and content blocks,
  `/v1/chat/completions` takes the gateway's own OpenAI shape unchanged, and
  `/openai/v1/responses` takes a *flat* tool spec (`name`/`parameters` beside
  `type`, no `function` wrapper) with tool traffic as top-level `function_call` /
  `function_call_output` items rather than as messages. The two non-passthrough
  routes reject OpenAI's shape outright (400 `Unexpected role "tool"` and 400
  `Invalid 'input'`), so the tool loop failed rather than degrading — except on
  Chat Completions, which answered fluently with the tools missing.
- Mantle's Responses route reported `finish_reason: "completed"` even when the
  model called a tool, because that field carries lifecycle status rather than a
  stop reason. A caller driving a tool loop reads that as "nothing left to do",
  so it returned the tool call to the client and never ran it.
- **`POST /v1/chat/completions` dropped tools entirely.** The pipeline had
  translated them per-provider since the work above, but the OpenAI-compatible
  route never read `tools`/`tool_choice` off the request body and `ClientAgent`
  flattened the response down to `content` — which a tool call has none of. So
  tool calling worked for in-process callers and not for the HTTP surface the
  README points OpenAI SDK clients at: another fluent 200 asserting no such tool
  existed. Both directions now carry tools, and `finish_reason` is no longer
  hardcoded to `"stop"`.
- **`finish_reason` leaked raw provider stop reasons.** With the hardcoded
  `"stop"` removed, values like Anthropic's `end_turn`, Gemini's `MAX_TOKENS` and
  Mantle's `completed` reached the client — and typed OpenAI SDK clients
  deserialize that field into an enum, so an unrecognized string is a client-side
  validation error rather than a curiosity. The OpenAI-compatible surface now
  maps every known provider reason onto the four legal values, defaulting to
  `"stop"`, while the internal API keeps reporting what the provider actually
  said.
- **Simulated streaming discarded tool calls.** `simulate_streaming` extracted
  `message.content` and nothing else, so a streamed tool call — which carries no
  text — produced a single empty chunk and vanished. This is the path every
  provider without true SSE takes (boto3 Bedrock, google_ai, and any provider
  whose stream fails to open), so `stream=True` plus `tools` returned an empty
  stream rather than an error. Tool calls now ride the final chunk, whole: their
  `arguments` are a JSON string, and word-splitting them would emit fragments no
  client can parse until reassembled.

## [0.1.0] - 2026-06-22

### Added
- Multi-provider LLM gateway with unified API (Bedrock, Anthropic, OpenAI, Azure, Vertex AI, Cohere)
- 5 routing strategies: round-robin, weighted, least-latency, cost-optimized, smart (intent-aware)
- Ensemble routing: scatter-gather-synthesize across model panels with configurable quorum
- Multi-region hub-and-spoke topology with automatic failover and data residency controls
- Policy hierarchy (org > BU > project > env) with budget, rate limit, and model restrictions
- Quota enforcement with per-user and per-project budget tracking and threshold alerts
- PII redaction with streaming token buffering for split-token detection
- Prompt injection detection with Unicode normalization (homoglyph-resistant)
- Immutable audit trail with SHA-256 hash chain
- Multi-strategy authentication: ALB OIDC JWT, Bearer token, API key
- API key management: issue, rotate, revoke, scope-restricted
- Admin RBAC with scoped access control
- Admin dashboard (9 pages) with token efficiency analytics
- Event dispatcher: webhook, SNS, CloudWatch Logs
- Semantic cache with LRU eviction
- Sliding window rate limiter (per-user and per-project)
- SSE streaming for all providers
- Docker and App Runner deployment support
- 980 tests (unit, integration, e2e, property-based)

### Security
- Race condition fixes in rate limiter, quota enforcer, and audit trail (asyncio.Lock)
- JWT signature verification enforced (removed insecure fallback)
- Admin RBAC resource-scoped authorization
- Bounded caches and buffers to prevent memory exhaustion
- Unicode NFKD normalization in injection detector
- Streaming PII buffer for cross-chunk token detection
- Right-to-left regex replacement to prevent token-on-token false matches
- Cache TTL enforcement in API key service
- Double-check locking for lazy DynamoDB table initialization
- Cryptographic randomness for routing selection (secrets module)
