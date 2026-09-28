"""Command-line entry point for ``dr-ansible``.

The ``audit``, ``returns`` and ``draft`` subcommands are added in later plan tasks.
"""

import click

from dr_ansible import __version__


@click.group()
@click.version_option(__version__, prog_name="dr-ansible")
def main() -> None:
    """Find Ansible modules with missing RETURN docs and draft them from evidence."""
