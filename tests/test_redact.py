"""Tests for redaction of observed values (plan task 17: FR-16, AC-12)."""

import json

import pytest

from dr_ansible.config import Config
from dr_ansible.mining.redact import (
    MAX_LENGTH,
    MAX_LINES,
    REDACTED,
    redact_sample,
    redact_value,
)
from dr_ansible.model import JSONValue, Sample

SECRET = "hunter2-s3cr3t"


def _serialised(value: object) -> str:
    return json.dumps(value)


# --- dropping whole samples ---------------------------------------------------------


def test_no_log_results_give_no_sample() -> None:
    assert redact_sample("ping", "pong", Config(), no_log=True) is None


@pytest.mark.parametrize(
    "path",
    [
        "password",
        "db_password",
        "api_token",
        "client_secret",
        "ssh_key",
        "creds.password",
    ],
)
def test_sensitive_paths_give_no_sample(path: str) -> None:
    assert redact_sample(path, SECRET, Config()) is None


def test_ordinary_values_are_kept() -> None:
    assert redact_sample("ping", "pong", Config()) == Sample("pong")
    assert redact_sample("count", 3, Config()) == Sample(3)
    assert redact_sample("info.size", 1024, Config()) == Sample(1024)


def test_null_is_a_real_sample() -> None:
    assert redact_sample("value", None, Config()) == Sample(None)


def test_custom_patterns_from_config() -> None:
    config = Config(redact_patterns=("^pin$",))
    assert redact_sample("pin", "1234", config) is None
    assert redact_sample("password", "x", config) == Sample("x")  # replaced default


# --- cleaning nested values ---------------------------------------------------------


def test_sensitive_nested_keys_keep_the_key_and_lose_the_value() -> None:
    value: JSONValue = {
        "user": "admin",
        "password": SECRET,
        "nested": {"api_token": SECRET, "ok": [1, 2]},
        "items": [{"secret": SECRET, "name": "a"}],
    }
    assert redact_value(value, Config()) == {
        "user": "admin",
        "password": REDACTED,
        "nested": {"api_token": REDACTED, "ok": [1, 2]},
        "items": [{"secret": REDACTED, "name": "a"}],
    }


def test_secrets_never_survive_anywhere_in_a_sample() -> None:
    value: JSONValue = {"a": [{"b": {"token": SECRET}}], "key": {"deep": SECRET}}
    sample = redact_sample("result", value, Config())
    assert sample is not None
    assert SECRET not in _serialised(sample.value)


def test_non_string_secrets_are_redacted_too() -> None:
    assert redact_value({"key": 12345, "secret": [1, 2]}, Config()) == {
        "key": REDACTED,
        "secret": REDACTED,
    }


# --- truncation ---------------------------------------------------------------------


def test_long_strings_are_truncated() -> None:
    long = "x" * (MAX_LENGTH + 50)
    result = redact_value(long, Config())
    assert isinstance(result, str)
    assert result == "x" * MAX_LENGTH + "..."


def test_strings_at_the_limit_are_kept() -> None:
    exact = "y" * MAX_LENGTH
    assert redact_value(exact, Config()) == exact


def test_file_contents_are_cut_to_a_few_lines() -> None:
    content = "\n".join(f"line {n}" for n in range(1, 11))
    result = redact_value(content, Config())
    assert result == "\n".join(f"line {n}" for n in range(1, MAX_LINES + 1)) + "\n..."


def test_short_multiline_text_is_kept() -> None:
    text = "a\nb\nc"
    assert MAX_LINES >= 3
    assert redact_value(text, Config()) == text


def test_strings_inside_containers_are_truncated() -> None:
    long = "z" * (MAX_LENGTH + 10)
    assert redact_value({"out": [long]}, Config()) == {
        "out": ["z" * MAX_LENGTH + "..."]
    }


def test_strings_within_the_marker_length_of_the_limit_are_kept() -> None:
    # Cutting these would not shorten them, and it keeps truncation idempotent.
    for extra in (1, 2, 3):
        text = "w" * (MAX_LENGTH + extra)
        assert redact_value(text, Config()) == text
    assert redact_value("w" * (MAX_LENGTH + 4), Config()) == "w" * MAX_LENGTH + "..."


# --- properties ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        12,
        1.5,
        True,
        None,
        [],
        {},
        {"password": SECRET, "x": "y" * (MAX_LENGTH * 2)},
        "\n".join(str(n) for n in range(20)),
    ],
)
def test_redaction_is_idempotent(value: JSONValue) -> None:
    once = redact_value(value, Config())
    assert redact_value(once, Config()) == once


def test_input_is_not_modified() -> None:
    value: JSONValue = {"password": SECRET, "list": ["a" * (MAX_LENGTH + 5)]}
    before = json.dumps(value)
    redact_value(value, Config())
    assert json.dumps(value) == before


def test_scalars_pass_through_unchanged() -> None:
    for scalar in (0, -1, 2.5, False, None, ""):
        assert redact_value(scalar, Config()) == scalar
