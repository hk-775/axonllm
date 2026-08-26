# Contributing to AxonLLM

Thank you for your interest in contributing to AxonLLM.

## Security

If you discover a potential security issue, please report it responsibly via the process described in [SECURITY.md](SECURITY.md). Do not create a public GitHub issue.

## Development Setup

We use [uv](https://docs.astral.sh/uv/). Install it if you haven't:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS / Linux
```

Then:

```bash
uv sync --extra dev   # creates .venv/, installs the package (import path: src.gateway) + test deps
```

Runtime deps (`uvicorn`, `boto3`, …) are declared in `pyproject.toml` and installed
by the line above — no separate install needed. Versions come from the committed
`uv.lock`, so your environment matches CI exactly.

Avoid bare `pip install -e ".[dev]"`: outside an activated virtualenv it installs
into your system Python and resolves to dependency *floors* (`httpx>=0.25.0` →
httpx 0.25.2), which can break unrelated packages in that environment.

If you change dependencies in `pyproject.toml`, run `uv lock` and commit the
updated `uv.lock` with your change. CI runs `uv lock --check` and will fail if the
lockfile is out of sync.

## Running Tests

```bash
uv run pytest tests/ -x -q
```

`uv run` syncs the environment before running, so this works without activating
the virtualenv.

The test suite includes both unit tests and Hypothesis property-based tests.

### Optional external containment suite

Standard AxonLLM CI is self-contained. Adopters and contributors do not need
Ostiari Escape Lab to build, test, or deploy AxonLLM.

Maintainers who want the additional cross-repository adversarial gate can enable
`.github/workflows/escape-lab.yml` with these repository variables:

- `AXON_ESCAPE_LAB_ENABLED=true`
- `AXON_ESCAPE_LAB_REPOSITORY=<owner/repository>`
- `AXON_ESCAPE_LAB_REF=<immutable 40-character commit SHA>`

They must also add `AXON_ESCAPE_LAB_TOKEN` as a repository secret. Use a
fine-grained token or GitHub App token with read-only contents access to only the
configured Escape Lab repository. Fork pull requests skip the optional
integration because GitHub intentionally withholds repository secrets.

## Code Style

- Python 3.11+
- Type hints on all public functions
- Docstrings on all modules and public classes/functions
- No hardcoded credentials or secrets — use environment variables or config files

## Pull Requests

1. Fork the repo and create your branch from `main`
2. Add tests for any new functionality
3. Ensure the full test suite passes
4. Update documentation if you changed APIs or configuration

## License

By contributing, you agree that your contributions will be licensed under the MIT-0 License.
