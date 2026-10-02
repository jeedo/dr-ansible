# Copyright: dr-ansible contributors
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Record what the module under test returns, for dr-ansible's ``--run`` (FR-14).

This file is loaded by ansible-core, usually inside an ansible-test container
where dr-ansible is not installed, so it imports nothing from dr_ansible and
carries its own copy of the redaction rules in ``dr_ansible/mining/redact.py``
(a test keeps the two in step).

Each result of the module under test is appended to the output file as one
JSON line::

    {"version": 1, "module": "ping", "state": "ok", "task": "...",
     "location": {"file": "...", "line": 4},
     "keys": [{"path": "ping", "type": "str", "value": "pong"}]}

Nested dicts give dotted paths (``stat.exists``); a dict with keys has no
``value`` of its own. Results of ``no_log`` tasks are skipped entirely. Keys
matching the redaction patterns keep their type but have no ``value``, and
sensitive keys nested inside a value are replaced by ``<redacted>`` (FR-16).
"""

from __future__ import annotations

DOCUMENTATION = """
name: dr_ansible_recorder
type: aggregate
short_description: Record the return values of one module for dr-ansible
description:
  - Appends one JSON line per result of the module under test to a file.
  - Used by C(dr-ansible returns --run) and C(dr-ansible draft --run).
requirements:
  - enable in configuration
options:
  output:
    description: File to append the JSON lines to. Nothing is recorded without it.
    type: str
    env:
      - name: DR_ANSIBLE_OUTPUT
    ini:
      - section: callback_dr_ansible_recorder
        key: output
  module_names:
    description: Names of the module under test; empty records every module.
    type: list
    elements: str
    default: []
    env:
      - name: DR_ANSIBLE_MODULE_NAMES
    ini:
      - section: callback_dr_ansible_recorder
        key: module_names
  redact_patterns:
    description: Case-insensitive regular expressions for keys whose values are dropped.
    type: list
    elements: str
    default: [key, password, secret, token]
    env:
      - name: DR_ANSIBLE_REDACT_PATTERNS
    ini:
      - section: callback_dr_ansible_recorder
        key: redact_patterns
"""

import json
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from ansible.plugins.callback import CallbackBase

#: Version of the JSON line format.
RECORD_VERSION = 1
#: Replaces the value of a sensitive key nested inside a value.
REDACTED = "<redacted>"
#: Longest string kept whole, in characters.
MAX_LENGTH = 120
#: Most lines of multi-line text kept.
MAX_LINES = 3
#: Deepest nesting of dicts turned into dotted paths.
MAX_DEPTH = 10
_ELLIPSIS = "..."
_ABSENT = object()


def compile_patterns(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


def _sensitive(key: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(key) for p in patterns)


def redact_value(value: Any, patterns: tuple[re.Pattern[str], ...]) -> Any:
    """A JSON-safe copy of ``value``, redacted and truncated as dr-ansible does."""
    if isinstance(value, Mapping):
        return {
            str(k): REDACTED
            if _sensitive(str(k), patterns)
            else redact_value(v, patterns)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item, patterns) for item in value]
    if isinstance(value, str):
        return _truncate(str(value))
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else str(value)
    return _truncate(str(value))


def _truncate(text: str) -> str:
    lines = text.split("\n")
    if len(lines) > MAX_LINES + 1 or (
        len(lines) == MAX_LINES + 1 and lines[-1] != _ELLIPSIS
    ):
        text = "\n".join([*lines[:MAX_LINES], _ELLIPSIS])
    # Strings within the marker's length of the limit are kept, so that
    # truncating twice gives the same result.
    if len(text) > MAX_LENGTH + len(_ELLIPSIS):
        text = text[:MAX_LENGTH] + _ELLIPSIS
    return text


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return "dict"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "list"
    return type(value).__name__


def _usable(key: Any) -> bool:
    return isinstance(key, str) and key != "" and "." not in key


class CallbackModule(CallbackBase):  # type: ignore[misc]  # ansible is untyped
    """Append each result of the module under test to a JSON lines file."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "dr_ansible_recorder"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._output = ""
        self._names: frozenset[str] = frozenset()
        self._patterns: tuple[re.Pattern[str], ...] = ()
        self.disabled = True

    def set_options(self, *args: Any, **kwargs: Any) -> None:
        super().set_options(*args, **kwargs)
        self.configure(
            output=self.get_option("output") or "",
            module_names=self.get_option("module_names") or [],
            redact_patterns=self.get_option("redact_patterns") or [],
        )

    def configure(
        self,
        *,
        output: str,
        module_names: Iterable[str],
        redact_patterns: Iterable[str],
    ) -> None:
        self._output = output
        self._names = frozenset(n.strip() for n in module_names if n.strip())
        self._patterns = compile_patterns(p for p in redact_patterns if p)
        self.disabled = not output

    # --- ansible's hooks ------------------------------------------------------------

    def v2_runner_on_ok(self, result: Any) -> None:
        if not _is_loop_summary(result):
            self._record(result, failed=False)

    def v2_runner_on_failed(self, result: Any, ignore_errors: bool = False) -> None:
        if not _is_loop_summary(result):
            self._record(result, failed=True)

    def v2_runner_item_on_ok(self, result: Any) -> None:
        self._record(result, failed=False)

    def v2_runner_item_on_failed(self, result: Any) -> None:
        self._record(result, failed=True)

    # --- recording ------------------------------------------------------------------

    def _record(self, result: Any, *, failed: bool) -> None:
        if self.disabled:
            return
        task = _task(result)
        names = {getattr(task, "action", None), getattr(task, "resolved_action", None)}
        if self._names and not (self._names & {n for n in names if n}):
            return
        data = _data(result)
        if getattr(task, "no_log", False) or data.get("_ansible_no_log"):
            return  # FR-16: nothing from a no_log task is kept

        if failed:
            state = "failed"
        elif data.get("changed"):
            state = "changed"
        else:
            state = "ok"
        keys: list[dict[str, Any]] = []
        self._flatten(data, "", 0, keys)
        record = {
            "version": RECORD_VERSION,
            "module": getattr(task, "action", None),
            "state": state,
            "task": task.get_name(),
            "location": _location(task),
            "keys": keys,
        }
        with open(self._output, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")

    def _flatten(
        self,
        data: Mapping[Any, Any],
        prefix: str,
        depth: int,
        keys: list[dict[str, Any]],
        hidden: bool = False,
    ) -> None:
        for key, value in data.items():
            if not _usable(key) or (not prefix and key.startswith("_ansible")):
                continue
            path = f"{prefix}{key}"
            secret = hidden or _sensitive(key, self._patterns)
            entry: dict[str, Any] = {"path": path, "type": _type_name(value)}
            nested = isinstance(value, Mapping) and value and depth < MAX_DEPTH
            if not secret and not nested:
                entry["value"] = redact_value(value, self._patterns)
            keys.append(entry)
            if nested:
                self._flatten(value, f"{path}.", depth + 1, keys, secret)


def _task(result: Any) -> Any:
    task = getattr(result, "task", None)
    return task if task is not None else result._task


def _data(result: Any) -> Mapping[str, Any]:
    data = getattr(result, "result", None)
    return data if isinstance(data, Mapping) else result._result


def _is_loop_summary(result: Any) -> bool:
    """The result for a whole loop: its items were recorded one by one."""
    task = _task(result)
    looped = getattr(task, "loop", None) is not None or getattr(task, "loop_with", None)
    return bool(looped) and "results" in _data(result)


def _location(task: Any) -> dict[str, Any] | None:
    path = task.get_path() or ""
    file, _, line = path.rpartition(":")
    if not file or not line.isdigit():
        return None
    return {"file": file, "line": int(line)}
