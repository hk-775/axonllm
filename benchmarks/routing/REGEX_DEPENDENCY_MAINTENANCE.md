# Regex evidence and dependency maintenance

On October 6, 2026, the post-merge dependency audit reported
CVE-2026-104874 / GHSA-54p9-h82j-f925 in the main project's `multidict`
6.7.1 dependency. The [upstream advisory](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925)
identifies 6.9.1 as the patched version. The main `uv.lock` and its exported
AgentCore `requirements.txt` now use 6.9.1. The optional four-router runtime
already used 6.9.1 and required no change.

The October 2 regex experiment included the then-current root `uv.lock`
in its immutable freeze. Its exact bytes are preserved as
[`docs/benchmarks/regex-revision-2026-10-02/uv.lock`](../../docs/benchmarks/regex-revision-2026-10-02/uv.lock),
with SHA-256
`8caaaa525d9ee67e68be05a79ad4004f8e6916ca86231d7ab8bc82b4368149cf`.
This file is historical evidence, not an installation target. The classifier,
runner, protocol, freeze manifest, corpus, and published predictions remain
unchanged.

CI verifies the archived lock against the original freeze, verifies every
other frozen file in place, and regrades the saved predictions. A separate
regression test runs both classifiers on all 1,200 prompts in the current
patched environment and requires every task label to match the recording.
That check does not replace the original measurements or remeasure latency.

The frozen evaluator deliberately rejects changed root dependencies. For
historical reproduction, use the recorded experiment commit:

```sh
git worktree add --detach ../axon-regex-october-2 \
  d5f7d6815cfe6412f3c37051090ff31148c40a03
```

The original protocol's commands apply in that checkout, whose dependency
snapshot includes the known vulnerability. For routine development and CI,
use the current checkout and patched lock instead:

```sh
uv sync --locked --extra dev --extra agentcore
uv run --locked --no-sync pytest tests/unit/test_regex_revision_evidence.py -q
```
