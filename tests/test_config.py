"""Tests for configuration loading (plan task 7: NFR-9, FR-6, FR-16, FR-18)."""

import re
from pathlib import Path

import pytest

from dr_ansible.config import (
    DEFAULT_COMMON_RETURN_KEYS,
    DEFAULT_DOCKER_IMAGE,
    DEFAULT_EXEMPT_MODULES,
    DEFAULT_REDACT_PATTERNS,
    Config,
    ConfigError,
    load_config,
)

# --- defaults ------------------------------------------------------------------


def test_default_exempt_modules_are_the_ac3_list() -> None:
    assert (
        frozenset(
            {
                "gather_facts",
                "import_playbook",
                "import_role",
                "import_tasks",
                "include_role",
                "include_tasks",
            }
        )
        == DEFAULT_EXEMPT_MODULES
    )


def test_default_common_return_keys_are_the_fr18_list() -> None:
    assert (
        frozenset(
            {
                "changed",
                "failed",
                "msg",
                "skipped",
                "invocation",
                "diff",
                "warnings",
                "deprecations",
                "exception",
            }
        )
        == DEFAULT_COMMON_RETURN_KEYS
    )


def test_default_redaction_patterns_are_the_fr16_words() -> None:
    assert DEFAULT_REDACT_PATTERNS == ("key", "password", "secret", "token")


def test_default_docker_image() -> None:
    assert DEFAULT_DOCKER_IMAGE == "default"


def test_no_config_file_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config == Config()
    assert config.source is None
    assert config.exempt_modules == DEFAULT_EXEMPT_MODULES
    assert config.common_return_keys == DEFAULT_COMMON_RETURN_KEYS
    assert config.redact_patterns == DEFAULT_REDACT_PATTERNS
    assert config.docker_image == DEFAULT_DOCKER_IMAGE


def test_config_is_immutable() -> None:
    with pytest.raises(AttributeError):
        Config().docker_image = "other"  # type: ignore[misc]


# --- sources ---------------------------------------------------------------------


def test_reads_dr_ansible_toml(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text('docker-image = "fedora42"\n')
    config = load_config(tmp_path)
    assert config.docker_image == "fedora42"
    assert config.source == tmp_path / "dr-ansible.toml"


def test_reads_tool_table_in_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.dr-ansible]\ndocker-image = "ubuntu2404"\n'
    )
    config = load_config(tmp_path)
    assert config.docker_image == "ubuntu2404"
    assert config.source == tmp_path / "pyproject.toml"


def test_pyproject_without_tool_table_gives_defaults(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    config = load_config(tmp_path)
    assert config == Config()
    assert config.source is None


def test_dr_ansible_toml_wins_over_pyproject_without_merging(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text('docker-image = "from-toml"\n')
    (tmp_path / "pyproject.toml").write_text(
        '[tool.dr-ansible]\ndocker-image = "from-pyproject"\n'
        'common-return-keys = ["changed"]\n'
    )
    config = load_config(tmp_path)
    assert config.docker_image == "from-toml"
    assert config.common_return_keys == DEFAULT_COMMON_RETURN_KEYS


def test_explicit_config_file(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere.toml"
    other.write_text('docker-image = "explicit"\n')
    (tmp_path / "dr-ansible.toml").write_text('docker-image = "ignored"\n')
    config = load_config(tmp_path, config_file=other)
    assert config.docker_image == "explicit"
    assert config.source == other


def test_explicit_pyproject_uses_its_tool_table(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.dr-ansible]\ndocker-image = "via-pyproject"\n')
    assert load_config(tmp_path, config_file=pyproject).docker_image == "via-pyproject"


def test_missing_explicit_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("nope.toml")):
        load_config(tmp_path, config_file=tmp_path / "nope.toml")


# --- replace and extend ------------------------------------------------------------


def test_list_replaces_the_default(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text(
        'exempt-modules = ["meta"]\ncommon-return-keys = ["changed", "msg"]\n'
    )
    config = load_config(tmp_path)
    assert config.exempt_modules == frozenset({"meta"})
    assert config.common_return_keys == frozenset({"changed", "msg"})


def test_extend_adds_to_the_default(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text(
        'extend-exempt-modules = ["meta", "ansible.builtin.set_fact"]\n'
        'extend-common-return-keys = ["rc"]\n'
        'extend-redact-patterns = ["api_?key"]\n'
    )
    config = load_config(tmp_path)
    assert config.exempt_modules == DEFAULT_EXEMPT_MODULES | {
        "meta",
        "ansible.builtin.set_fact",
    }
    assert config.common_return_keys == DEFAULT_COMMON_RETURN_KEYS | {"rc"}
    assert config.redact_patterns == (*DEFAULT_REDACT_PATTERNS, "api_?key")


def test_extend_applies_after_replace(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text(
        'redact-patterns = ["pin"]\nextend-redact-patterns = ["otp", "pin"]\n'
    )
    assert load_config(tmp_path).redact_patterns == ("pin", "otp")


def test_empty_list_clears_the_default(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text("common-return-keys = []\n")
    assert load_config(tmp_path).common_return_keys == frozenset()


# --- validation --------------------------------------------------------------------


def test_invalid_toml_is_an_error_naming_the_file(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text("docker-image = \n")
    with pytest.raises(ConfigError, match=r"dr-ansible\.toml"):
        load_config(tmp_path)


def test_unknown_key_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text('exempt_modules = ["meta"]\n')
    with pytest.raises(ConfigError, match="exempt_modules"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "line",
    [
        'exempt-modules = "meta"',
        "common-return-keys = [1, 2]",
        "docker-image = 3",
        'docker-image = ""',
        'extend-redact-patterns = [""]',
    ],
)
def test_wrong_types_are_errors(tmp_path: Path, line: str) -> None:
    (tmp_path / "dr-ansible.toml").write_text(line + "\n")
    with pytest.raises(ConfigError, match=line.split(" ", 1)[0]):
        load_config(tmp_path)


def test_bad_regex_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "dr-ansible.toml").write_text('redact-patterns = ["pass("]\n')
    with pytest.raises(ConfigError, match=r"pass\("):
        load_config(tmp_path)


def test_tool_table_must_be_a_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool]\ndr-ansible = "x"\n')
    with pytest.raises(ConfigError, match=re.escape("tool.dr-ansible")):
        load_config(tmp_path)


def test_config_error_is_a_value_error() -> None:
    assert issubclass(ConfigError, ValueError)


# --- redaction helper ------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("password", True),
        ("db_PASSWORD", True),
        ("api_token", True),
        ("client_secret", True),
        ("ssh_key", True),
        ("keyfile", True),
        ("dest", False),
        ("checksum", False),
    ],
)
def test_is_sensitive_matches_patterns_case_insensitively(
    key: str, sensitive: bool
) -> None:
    assert Config().is_sensitive(key) is sensitive
