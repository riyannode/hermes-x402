"""Shared test fixtures for hermes-x402.

Mocks DNS validation to succeed by default for existing tests.
DNS validation is tested specifically in test_hardening.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_dns_validation():
    """Mock DNS validation to succeed for all tests by default.

    The DNS validation is imported lazily inside tool handlers via:
        from hermes_x402.dns_validator import resolve_and_validate_destination

    So we mock it at the dns_validator module level.
    """
    with patch(
        "hermes_x402.dns_validator.resolve_and_validate_destination",
        new_callable=AsyncMock,
        return_value=("93.184.216.34",),
    ):
        yield
