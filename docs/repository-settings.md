# Repository settings

Some of dr-ansible's safeguards are GitHub repository settings rather than files, so a pull request
cannot turn them on. A repository owner enables them by hand, once.

## Dependabot security updates

`.github/dependabot.yml` sets up weekly *version* updates. Security updates, which open a pull request
as soon as an advisory affects a dependency, are a separate setting:

- Settings → Code security → **Dependabot alerts**: enable.
- Settings → Code security → **Dependabot security updates**: enable.

## Branch protection for `main`

CI (`.github/workflows/ci.yml`) runs on every pull request, Dependabot's included, but it only *guards*
`main` once branch protection requires it. Under Settings → Branches, add a rule for `main`:

- **Require a pull request before merging**, with at least one approving review.
- **Require status checks to pass**, with *Require branches to be up to date* on, and these checks
  selected:
  - `test`: lint, format, type check, unit tests and the docs check;
  - `acceptance`: the acceptance tests against the pinned ansible-core checkout.
- **Do not allow bypassing the above settings**, including for administrators.
- **Restrict force pushes and deletions** on `main`.

The runtime tests (`.github/workflows/runtime.yml`) run on demand and weekly, not on pull requests, so
they are not a required check.

## Releases

Publishing to PyPI needs trusted publishers on PyPI and TestPyPI and two GitHub
environments, `pypi` and `testpypi`: see [releasing.md](releasing.md).

## No auto-merge

Leave auto-merge off. Dependabot pull requests pass the same checks as anyone's and still need a human
approval: that review is what makes an automated dependency bump safe to accept.
