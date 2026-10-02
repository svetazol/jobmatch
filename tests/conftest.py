import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    """One asyncio loop for the whole session.

    The engine is module-level and its pooled connections belong to the loop
    that opened them; a loop per test would leave each test holding the last
    one's dead connections.
    """
    return "asyncio"
