# AxonLLM Review — Remediation TODO

Working backlog from the strict product/security/architecture review.
Snapshot as of **2026-07-22**. Internal only (not for open-source distribution —
see `docs/internal/README.md`).

## Status summary

- **Every review blocker and every self-contained, unit-testable item is done.**
- **Merged this session (7 PRs, items #16, #17, #18, #15a/e/c, #12, #14):** see
  "Completed" below. Earlier sessions closed #2, #3, #4, #7, #8, #9, #10, #11, #13.
- **Remaining work is deploy-coupled** (needs a real multi-replica AWS deploy to
  validate) **or human-only** (#1 key rotation). Building these blind isn't
  worthwhile — batch them for when an AWS environment is available.
- Every merged change was verified in **both run modes**: standalone AND embedded
  in Ostiari (`import src.gateway`), plus `ruff` + full pytest on 3.11/3.12 CI.

---

## ✅ Completed (this session)

### #16 — Audit trail durability + SIEM export  `[merged, PR #13]`
Chain head reloads from the durable store on startup (survives restarts);
`verify_persisted_chain` detects tampering + row removal in the durable store;
`/admin/audit/export` for SIEM/S3. Loop-safe `initialize_sync` for the embedded
(running-loop) case.

### #17 — PII redaction hardening + real OTEL  `[merged, PRs #14 + #15]`
- **PII (PR #14):** env-gated safe-by-default (`AXON_PII_REDACTION_DEFAULT`),
  multimodal/list-content coverage, intl patterns (iban/passport/ipv6), optional
  permanent-redaction (`pii_reinject=false`, no plaintext retained). Env-gated so
  the Ostiari embed (which redacts at its own layer) never double-redacts.
- **OTEL (PR #15):** native OTLP span exporter (`gen_ai.*` + `axon.*`), opt-in via
  `OTEL_EXPORTER_OTLP_ENDPOINT`, `BatchSpanProcessor` (non-blocking). Suppressed
  when embedded — Ostiari emits the governance span there (one span per request).

### #18 — True end-to-end token streaming  `[merged, PR #16]`
Was a double provider call (blocking + a second streaming call). Now opens the
provider SSE stream directly: real TTFT, one call, end-of-stream cost/audit,
provider-reported usage (OpenAI `include_usage`, Anthropic message events) with
tiktoken fallback, pre-first-byte fallback only, PII reinject across chunks.
Ensemble unchanged (can't stream mid-panel).

### #15 (a/e/c) — Reliability hardening  `[merged, PR #17]`
- (a) `cost_tracker` running per-project/user spend counters → O(1) budget checks
  that survive record-list trimming (trimming no longer under-counts budgets).
- (e) `StripedLock` per-key locks in rate_limiter + quota_enforcer (no global-lock
  serialization; rate limiter locks user+project keys in sorted order).
- (c) jittered exponential backoff (`RetryConfig.jitter`) to break retry storms.

### #12 — Multi-region routing made real  `[merged, PR #18]`
`target_spoke` now threads through to the provider call (endpoint overrides
base_url; region rewrites SigV4 cred + binds a per-region Bedrock client);
`SpokeHealthMonitor` started from the app lifespan when >1 spoke; `spokes.yaml`
loader with single-region fallback. Wired through direct/smart/streaming paths.

### #14 — SAML SSO + SCIM 2.0 provisioning  `[merged, PR #19]`
- **SAML 2.0 (SP side):** `/saml/login|acs|metadata`; pure-Python signed-assertion
  verification (`signxml` + lxml + `cryptography`, wheels only — no xmlsec1);
  audience + NotOnOrAfter enforced; XXE/DOCTYPE blocked. `AXON_SAML_*` config.
- **SCIM 2.0:** `/scim/v2/Users` + `/scim/v2/Groups` (list+filter+pagination, full
  CRUD, PATCH `active=false` deprovision); persistence-backed store, roles = user ∪
  group. `AXON_SCIM_TOKEN`-gated. Deps lazily imported → embed unaffected.

---

## ⚠️ Do first — human-only

### #1 — Rotate exposed provider API keys + scrub GitLab history  `[URGENT, human]`
- Real-format keys were exposed in the internal GitLab remote history
  (`gitlab.aws.dev/harlnk/LLM-Router`) and in cleartext on local disk. Public
  GitHub (`hk-775/axonllm`) history is **clean**.
- **Action:** rotate every key; scrub GitLab history (git-filter-repo / BFG) or
  delete+recreate. Treat all current keys as compromised. Blocks #6.

---

## Deploy-coupled — best validated on real AWS (not built blind)

### #5 — Shared enforcement state + rehydrate on restart  `[pending, blocked by #4]`
Spend/rate/cache counters are per-replica in-memory: budgets over-spend across
replicas and reset on restart. Needs a shared store (DynamoDB atomic counters /
Redis) + startup rehydration. Validate on a multi-replica deploy.
*(Note: #17-PR#17 added in-process spend counters + rehydrate-from-history; the
cross-replica atomic-counter story is the remaining, deploy-coupled part.)*

### #6 — Provider secrets → Secrets Manager  `[pending, blocked by #1]`
Load provider creds from AWS Secrets Manager / SSM (not plaintext
`.env`/`providers.yaml`); refuse to boot if a real secret is committed to config.
Do #1 (rotate) first.

### #15 (b) — Cross-replica API-key revocation  `[pending, deploy-coupled]`
Revocation lags up to 5 min across replicas (per-replica cache, no cross-instance
invalidation). Needs shared invalidation (pub/sub or shared store) — overlaps #5.

### #15 (d) — Dockerfile hardening  `[pending, validate on a real build]`
Unpinned `pip install` that ignores pyproject and never installs the package,
runs as root, no HEALTHCHECK. Best fixed + validated against an actual image build.

### #12-follow / #19 — Scheduled S3 usage export + CUR/FinOps mapping  `[pending]`
`/admin/usage/export` (CSV/JSON) shipped earlier. Remaining: scheduled S3 export
(bucket/prefix/cadence + `s3:PutObject` IAM) and an AxonLLM-usage → AWS CUR /
cost-allocation-tag mapping. Reuse the existing row-builder.

---

## Conventions

- Branch off `main` → PR → wait for CI (3.11 + 3.12) → squash-merge → delete branch.
- Before every PR: `.venv/bin/python -m pytest tests/ -q` and `ruff check src/ tests/`.
- Verify both modes: standalone AND embedded in Ostiari (`PYTHONPATH` includes both
  repos; run Ostiari's `gateway/tests`).
- `docs/internal/` in THIS repo is git-tracked (unlike Ostiari's).
