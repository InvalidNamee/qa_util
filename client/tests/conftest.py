import pytest
import pytest_asyncio

from qa_util.config import ClientConfig
from qa_util.models import Account


@pytest.fixture
def config(tmp_path):
    return ClientConfig("http://server.test", "client-secret-token", Account("a", "password-a"), tmp_path / "client-a")


@pytest_asyncio.fixture
async def browser():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        instance = await p.chromium.launch(headless=True)
        yield instance
        await instance.close()
