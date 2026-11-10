import pytest

from qa_util.config import ClientConfig
from qa_util.models import Account


@pytest.fixture
def config(tmp_path):
    return ClientConfig("http://server.test", "client-secret-token", Account("a", "password-a"), tmp_path / "client-a")
