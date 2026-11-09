import pytest

from qa_util.config import ClientConfig
from qa_util.models import Account
from server_support import app, llm, service, settings  # noqa: F401


@pytest.fixture
def config(tmp_path):
    return ClientConfig("http://server.test", "client-secret-token", Account("a", "password-a"), tmp_path / "client-a")
