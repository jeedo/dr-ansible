# Releasing

dr-ansible is published to PyPI as [`dr-ansible`](https://pypi.org/p/dr-ansible) by
`.github/workflows/release.yml`, when a version tag is pushed. Publishing uses PyPI's
[trusted publishing](https://docs.pypi.org/trusted-publishers/): GitHub proves the upload
comes from this workflow, so no API token is stored anywhere.

## One-time setup

A repository owner does this once, before the first release.

1. **Register the trusted publisher on PyPI.** At
   <https://pypi.org/manage/account/publishing/>, add a *pending publisher*:
   - PyPI project name: `dr-ansible`
   - Owner: `jeedo`, repository: `dr-ansible`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. **Do the same on TestPyPI** at <https://test.pypi.org/manage/account/publishing/>, with
   environment name `testpypi`. (TestPyPI is a separate service with its own account.)
3. **Create the two environments** under Settings → Environments:
   - `testpypi`: no protection rules needed.
   - `pypi`: add yourself under **Required reviewers**, and under **Deployment branches and
     tags** allow only tags matching `v*`. Every PyPI upload then waits for your approval.

The pending publishers turn into normal ones when the first release creates the projects.

## Making a release

1. Bump the version on a branch and merge it to `main` through a pull request:

   ```bash
   uv version --bump minor      # or patch / major; updates pyproject.toml and uv.lock
   ```

2. Tag the merged commit on `main` and push the tag:

   ```bash
   git tag v$(uv version --short)
   git push origin v$(uv version --short)
   ```

3. The workflow then, in order:
   - checks the tag matches the package version (a mismatch stops everything);
   - runs the full CI suite on the tagged commit (`ci.yml`: lint, types, tests, acceptance);
   - builds the wheel and sdist once with `uv build`;
   - publishes them to TestPyPI;
   - **waits for your approval** of the `pypi` environment, then publishes to PyPI, with
     PEP 740 attestations;
   - creates a GitHub release for the tag with the built files attached.

4. Check <https://test.pypi.org/p/dr-ansible> if you like, then approve the `pypi` deployment
   from the workflow run page.

A failed run publishes nothing after the failing step. PyPI never accepts the same version
twice, so to retry after a PyPI upload, bump the version and tag again.

## Checking a release

Once it is on PyPI, check that every install path gives a working `dr-ansible` (NFR-2). Use
Python 3.13 and a throwaway directory; `PATH_TO_ANSIBLE` is any ansible-core checkout.

```bash
V=0.2.0   # the version just released
uvx dr-ansible@$V --version
uv tool install dr-ansible==$V && dr-ansible --version
pipx install dr-ansible==$V && dr-ansible --version
python3.13 -m venv venv && venv/bin/pip install "dr-ansible[rich]==$V" && venv/bin/dr-ansible --version
uvx --from git+https://github.com/jeedo/dr-ansible dr-ansible --version
git clone https://github.com/jeedo/dr-ansible && cd dr-ansible && uv sync && uv run dr-ansible --version
pip install -e .   # in the clone, inside a Python 3.13 virtual environment
```

Each should print the new version, and `dr-ansible audit PATH_TO_ANSIBLE --module ping,fetch`
should list both modules and exit with 1 (fetch has no `RETURN`). On Python 3.12 or older, pip
refuses to install it (`Requires-Python >=3.13`).

v0.2.0 was checked this way on 2026-10-03: all the paths above worked.
