"""The demo .env loader must never shadow a real deploy's secrets.

Two properties carry the safety of this feature, and both are asserted here
directly rather than inferred from the happy path: it loads only when the
operator explicitly asked for demo mode, and an existing environment variable
always wins over the file. The second is the one that matters — it means the
loader is inert even if it runs somewhere it shouldn't, which is the realistic
failure mode given that the Dockerfile CMD is the same entrypoint that enables
demo mode by default.
"""

from __future__ import annotations

from src.gateway.dev_env import demo_env_requested, load_dev_env_file, parse_env_file

_KEY = "OPENAI_API_KEY"


def _write(tmp_path, text: str) -> str:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestEnvironmentAlwaysWins:
    """The property that makes the file safe to ship."""

    def test_existing_value_is_not_overwritten(self, tmp_path):
        env = {"AXON_LOAD_DEMO_DATA": "true", _KEY: "from-the-platform"}
        loaded = load_dev_env_file(_write(tmp_path, f"{_KEY}=from-the-file"), env)
        assert env[_KEY] == "from-the-platform"
        assert _KEY not in loaded

    def test_partial_overlap_loads_only_the_missing_names(self, tmp_path):
        """A deploy injecting one key must not have it replaced while still
        picking up nothing else it didn't ask for."""
        env = {"AXON_LOAD_DEMO_DATA": "true", _KEY: "from-the-platform"}
        loaded = load_dev_env_file(
            _write(tmp_path, f"{_KEY}=from-the-file\nXAI_API_KEY=xai-file"), env
        )
        assert env[_KEY] == "from-the-platform"
        assert env["XAI_API_KEY"] == "xai-file"
        assert loaded == ["XAI_API_KEY"]

    def test_empty_value_in_file_does_not_clear_anything(self, tmp_path):
        env = {"AXON_LOAD_DEMO_DATA": "true"}
        loaded = load_dev_env_file(_write(tmp_path, f"{_KEY}="), env)
        assert _KEY not in env
        assert loaded == []


class TestDemoModeGate:
    def test_no_flag_means_no_load(self, tmp_path):
        """The realistic production case: nothing set, file present anyway."""
        env: dict[str, str] = {}
        loaded = load_dev_env_file(_write(tmp_path, f"{_KEY}=from-the-file"), env)
        assert loaded == []
        assert _KEY not in env

    def test_flag_false_means_no_load(self, tmp_path):
        env = {"AXON_LOAD_DEMO_DATA": "false"}
        loaded = load_dev_env_file(_write(tmp_path, f"{_KEY}=from-the-file"), env)
        assert loaded == []
        assert _KEY not in env

    def test_flag_true_loads(self, tmp_path):
        env = {"AXON_LOAD_DEMO_DATA": "true"}
        loaded = load_dev_env_file(_write(tmp_path, f"{_KEY}=from-the-file"), env)
        assert env[_KEY] == "from-the-file"
        assert loaded == [_KEY]

    def test_flag_is_case_and_whitespace_tolerant(self):
        assert demo_env_requested({"AXON_LOAD_DEMO_DATA": " TRUE "}) is True
        assert demo_env_requested({"AXON_LOAD_DEMO_DATA": "1"}) is False
        assert demo_env_requested({}) is False

    def test_missing_file_is_not_an_error(self, tmp_path):
        env = {"AXON_LOAD_DEMO_DATA": "true"}
        assert load_dev_env_file(str(tmp_path / "absent.env"), env) == []

    def test_unreadable_path_is_not_an_error(self, tmp_path):
        """A directory where a file was expected must not crash startup."""
        env = {"AXON_LOAD_DEMO_DATA": "true"}
        assert load_dev_env_file(str(tmp_path), env) == []


class TestReturnValueCarriesNoSecrets:
    def test_returns_names_not_values(self, tmp_path):
        env = {"AXON_LOAD_DEMO_DATA": "true"}
        loaded = load_dev_env_file(_write(tmp_path, f"{_KEY}=sk-secret-value"), env)
        assert loaded == [_KEY]
        assert not any("sk-secret-value" in item for item in loaded)


class TestParsing:
    def test_comments_and_blanks_ignored(self):
        parsed = parse_env_file("# a comment\n\n  \nA=1\n")
        assert parsed == {"A": "1"}

    def test_export_prefix_stripped(self):
        assert parse_env_file("export A=1") == {"A": "1"}

    def test_quoted_values_unwrapped(self):
        assert parse_env_file("A='one'\nB=\"two\"") == {"A": "one", "B": "two"}

    def test_hash_inside_quoted_value_preserved(self):
        """An API key may legitimately contain a '#'; truncating it would
        produce an invalid credential and a confusing 401."""
        assert parse_env_file('A="sk-ab#cd"') == {"A": "sk-ab#cd"}

    def test_unquoted_trailing_comment_stripped(self):
        assert parse_env_file("A=value  # trailing") == {"A": "value"}

    def test_hash_without_preceding_space_kept(self):
        assert parse_env_file("A=sk-ab#cd") == {"A": "sk-ab#cd"}

    def test_value_containing_equals_preserved(self):
        """Base64-ish secrets end in '='; partition on the first separator only."""
        assert parse_env_file("A=abc=def==") == {"A": "abc=def=="}

    def test_lines_without_equals_skipped(self):
        assert parse_env_file("not an assignment\nA=1") == {"A": "1"}

    def test_non_identifier_names_skipped(self):
        assert parse_env_file("not a name=1\nA=1") == {"A": "1"}

    def test_surrounding_whitespace_trimmed(self):
        assert parse_env_file("  A = 1  ") == {"A": "1"}
