import pytest

from app import create_app
from app.config import AppConfig


@pytest.fixture
def app_config(tmp_path):
    config = AppConfig()
    config.database_uri = "sqlite:///:memory:"
    config.stores_dir = tmp_path / "stores"
    config.stores_dir.mkdir()
    return config


@pytest.fixture
def app(app_config):
    flask_app = create_app(app_config)
    flask_app.config["TESTING"] = True
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()
