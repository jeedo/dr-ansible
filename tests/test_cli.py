"""Tests for the package skeleton and the stub ``dr-ansible`` CLI (plan task 1)."""

import shutil
import subprocess
from importlib.metadata import entry_points, version

from click.testing import CliRunner

import dr_ansible
from dr_ansible.cli import main


def test_package_exposes_installed_version() -> None:
    assert dr_ansible.__version__ == version("dr-ansible")


def test_console_script_points_at_cli_main() -> None:
    (script,) = entry_points(group="console_scripts", name="dr-ansible")
    assert script.value == "dr_ansible.cli:main"


def test_help_lists_program_name() -> None:
    result = CliRunner().invoke(main, ["--help"], prog_name="dr-ansible")
    assert result.exit_code == 0
    assert "Usage: dr-ansible [OPTIONS] COMMAND [ARGS]..." in result.output


def test_version_option_prints_version() -> None:
    result = CliRunner().invoke(main, ["--version"], prog_name="dr-ansible")
    assert result.exit_code == 0
    assert result.output.strip() == f"dr-ansible, version {dr_ansible.__version__}"


def test_unknown_command_is_a_usage_error() -> None:
    result = CliRunner().invoke(main, ["no-such-command"])
    assert result.exit_code == 2


def test_installed_console_script_runs() -> None:
    exe = shutil.which("dr-ansible")
    assert exe is not None
    proc = subprocess.run([exe, "--help"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "Usage: dr-ansible" in proc.stdout
