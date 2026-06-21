# Contributing to AxonLLM

Thank you for your interest in contributing to AxonLLM.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## Security Issue Notifications

If you discover a potential security issue, please notify us privately. Do not create a public GitHub issue.

## Development Setup

```bash
pip install -e ".[dev]"
pip install uvicorn boto3
```

## Running Tests

```bash
pytest tests/ -x -q
```

The test suite includes both unit tests and Hypothesis property-based tests.

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
