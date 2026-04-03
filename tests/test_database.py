import importlib
import sys


def reload_database_module():
    sys.modules.pop("database", None)
    return importlib.import_module("database")


def test_database_url_normalizes_postgres_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/hotelapi")

    database = reload_database_module()

    assert database.settings.database_url.startswith("postgresql://")


def test_database_settings_support_lowercase_aliases(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("database_url", "sqlite:///./alias-test.db")
    monkeypatch.setenv("auto_create_schema", "true")

    database = reload_database_module()

    assert database.settings.database_url == "sqlite:///./alias-test.db"
    assert database.settings.auto_create_schema is True
