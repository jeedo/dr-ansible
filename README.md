# dr-ansible

Documentation help for Ansible: find modules with missing or incomplete
`RETURN` documentation, work out which keys they actually return, and print a
draft `RETURN` block for a human to finish.

> **Status**: alpha. The `audit`, `returns` and `draft` commands work; see the docs below.

- [Requirements](docs/requirements.md)
- [Architecture](docs/architecture.md) (approved)
- [Implementation plan](docs/plan.md) (approved)
- [Research notes](docs/research.md)

This project follows the spec-driven workflow from
[jeedo/spec-template](https://github.com/jeedo/spec-template); see
[CLAUDE.md](CLAUDE.md).

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). dr-ansible imports ansible-core, which is GPLv3.
