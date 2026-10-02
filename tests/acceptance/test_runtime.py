"""``--run`` end to end: AC-11 and AC-12 (plan task 33).

Each test runs in two modes: ``--local`` (ansible-test on this machine) and
``--docker default`` (the default, in ansible-test's container). The Docker
mode is skipped when Docker is not available. These tests start real
``ansible-test integration`` runs, so they are marked ``runtime`` as well as
``acceptance``: ``uv run pytest -m runtime``.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from dr_ansible.cli import main

pytestmark = [pytest.mark.acceptance, pytest.mark.runtime]


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(["docker", "info"], capture_output=True, check=False)
    return proc.returncode == 0


@pytest.fixture(params=["local", "docker"])
def mode(request: pytest.FixtureRequest) -> list[str]:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ansible-test needs ssh-keygen")
    if request.param == "docker":
        if not _docker_available():
            pytest.skip("Docker is not available")
        return ["--run", "--docker", "default"]
    return ["--run", "--local"]


def _invoke(*args: str | Path) -> Result:
    return CliRunner().invoke(main, [str(a) for a in args])


def _module_json(result: Result) -> dict[str, Any]:
    (module,) = json.loads(result.stdout)["modules"]
    assert isinstance(module, dict)
    return module


# --- AC-11: ping records ping as str with sample pong -------------------------------


def test_ac11_ping(ansible_core: Path, mode: list[str]) -> None:
    result = _invoke("returns", ansible_core, "ping", "--format", "json", *mode)
    assert result.exit_code == 0, result.output
    ping = {k["name"]: k for k in _module_json(result)["keys"]}["ping"]
    assert ping["status"] == "ok"
    observed = ping["observed"]
    assert observed["types"] == ["str"]
    assert observed["sample"] == "pong"
    assert "ok" in observed["results"]
    assert "ansible-test recorded" in result.stderr


def test_ac11_draft_uses_the_runtime_sample(
    ansible_core: Path, mode: list[str]
) -> None:
    result = _invoke("draft", ansible_core, "ping", "--full", *mode)
    assert result.exit_code == 0, result.output
    entry = result.stdout.split("\nping:\n", 1)[1].split('\n"""', 1)[0]
    assert "    type: str\n" in entry
    assert "    sample: pong" in entry


# --- AC-12: nothing secret reaches the output ---------------------------------------

MODULE = """\
#!/usr/bin/python
from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict(data=dict(type="str", default="x")))
    module.exit_json(
        changed=False,
        password="hunter2-secret",
        info={"api_token": "tok-123-secret", "user": "someone"},
        echo=module.params["data"],
        plain="visible-value",
    )


if __name__ == "__main__":
    main()
"""
TASKS = """\
- ns.coll.secretive:
  register: first
- ns.coll.secretive:
    data: nolog-echo-secret
  no_log: true
  register: hidden
- assert:
    that:
      - first.plain == 'visible-value'
      - hidden.echo is defined
"""
SECRETS = ("hunter2-secret", "tok-123-secret", "nolog-echo-secret")


@pytest.fixture
def collection(tmp_path: Path) -> Path:
    root = tmp_path / "ansible_collections" / "ns" / "coll"
    (root / "plugins" / "modules").mkdir(parents=True)
    (root / "galaxy.yml").write_text(
        "namespace: ns\nname: coll\nversion: 1.0.0\nreadme: README.md\nauthors: [x]\n"
    )
    (root / "README.md").write_text("")
    (root / "plugins" / "modules" / "secretive.py").write_text(MODULE)
    tasks = root / "tests" / "integration" / "targets" / "secretive" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(TASKS)
    return root


def test_ac12_secrets_never_reach_the_output(collection: Path, mode: list[str]) -> None:
    outputs = []
    for fmt in ("json", "table", "markdown"):
        result = _invoke("returns", collection, "secretive", "--format", fmt, *mode)
        assert result.exit_code == 1, result.output  # everything is undocumented
        outputs.append(result.stdout)
    draft = _invoke("draft", collection, "secretive", *mode)
    assert draft.exit_code == 0, draft.output
    outputs.append(draft.stdout)
    for output in outputs:
        for secret in SECRETS:
            assert secret not in output, secret

    keys = {k["name"]: k for k in _module_json(_json_run(collection, mode))["keys"]}
    # The run did record: the harmless value is there as a sample.
    assert keys["plain"]["observed"]["sample"] == "visible-value"
    # Sensitive keys are recorded without a sample.
    assert keys["password"]["observed"]["types"] == ["str"]
    assert "sample" not in keys["password"]["observed"]
    assert "sample" not in keys["info.api_token"]["observed"]
    assert keys["info.user"]["observed"]["sample"] == "someone"
    # The no_log result was skipped entirely: echo's sample comes from `first`.
    # (Its count also includes the static read of hidden.echo in the assert.)
    assert keys["echo"]["observed"]["sample"] == "x"


def _json_run(collection: Path, mode: list[str]) -> Result:
    return _invoke("returns", collection, "secretive", "--format", "json", *mode)
