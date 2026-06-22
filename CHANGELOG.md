# Changelog

All notable changes to AxonLLM will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
