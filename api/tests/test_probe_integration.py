import os

import httpx
import pytest

# Read at import time: conftest's autouse fixture overwrites SUPABASE_URL per test.
STACK_URL = os.environ.get("SUPABASE_URL", "")


@pytest.mark.integration
def test_stack_is_reachable_with_exported_keys() -> None:
    assert STACK_URL, "SUPABASE_URL was not exported"
    assert os.environ.get("SUPABASE_PUBLISHABLE_KEY"), "publishable key was not exported"
    response = httpx.get(f"{STACK_URL.rstrip('/')}/auth/v1/health", timeout=30)
    assert response.status_code == 200, response.text
