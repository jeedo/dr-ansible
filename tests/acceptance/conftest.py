"""Fixtures for acceptance tests: the pinned ansible-core checkout."""

from pathlib import Path

import pytest

from tests.acceptance.checkout import CheckoutError, resolve_checkout


@pytest.fixture(scope="session")
def ansible_core() -> Path:
    """The pinned ansible-core devel checkout, fetched once per cache."""
    try:
        return resolve_checkout()
    except CheckoutError as exc:
        pytest.fail(f"acceptance tests need the ansible-core checkout: {exc}")
