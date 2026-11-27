import os
import tempfile

# Banco SQLite temporário, definido antes de importar o app (o app lê
# DATABASE_URL no import). Nunca toca no servico_cidade.db do projeto.
_fd, _caminho = tempfile.mkstemp(suffix=".db", prefix="socorreaqui-test-")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _caminho.replace("\\", "/")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
