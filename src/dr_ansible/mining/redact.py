"""Keep sensitive and oversized values out of samples (FR-16, AC-12).

Applied wherever an observed value could reach output: in the runtime
callback and again before reports and drafts are written.

- A ``no_log`` result, or a key whose path has a segment matching the
  redaction patterns (``password``, ``token``, ``secret``, ``key`` by
  default, see :class:`~dr_ansible.config.Config`), gives no sample at all.
- Inside a kept value, a sensitive nested key keeps its name but its value
  becomes :data:`REDACTED`, so the shape of the sample stays visible.
- Strings longer than :data:`MAX_LENGTH` and text longer than
  :data:`MAX_LINES` lines (file contents) are cut, ending in ``...``.

Redaction is idempotent and never modifies its input.
"""

from dr_ansible.config import Config
from dr_ansible.model import JSONValue, Sample

#: Replaces the value of a sensitive key nested inside a sample.
REDACTED = "<redacted>"
#: Longest string kept whole, in characters.
MAX_LENGTH = 120
#: Most lines of multi-line text (such as file contents) kept.
MAX_LINES = 3
_ELLIPSIS = "..."


def redact_sample(
    path: str, value: JSONValue, config: Config, *, no_log: bool = False
) -> Sample | None:
    """The sample to keep for the key ``path``, or ``None`` if it must be dropped."""
    if no_log:
        return None
    if any(config.is_sensitive(segment) for segment in path.split(".")):
        return None
    return Sample(redact_value(value, config))


def redact_value(value: JSONValue, config: Config) -> JSONValue:
    """A copy of ``value`` with sensitive nested values replaced and text cut."""
    if isinstance(value, dict):
        return {
            key: REDACTED if config.is_sensitive(key) else redact_value(item, config)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, config) for item in value]
    if isinstance(value, str):
        return _truncate(value)
    return value


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
