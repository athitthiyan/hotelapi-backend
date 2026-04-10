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


def test_production_runtime_configuration_requires_strong_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./prod-test.db")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "short-key")

    database = reload_database_module()

    try:
        database.validate_runtime_configuration(database.settings)
        assert False, "Expected production config validation to fail"
    except RuntimeError as exc:
        assert "Production SECRET_KEY must be at least 64 characters long" in str(exc)


def test_settings_env_file_prefers_explicit_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STAYVORA_ENV_FILE", ".env.custom")

    database = reload_database_module()

    assert database.select_settings_env_file() == ".env.custom"


def test_settings_env_file_prefers_local_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STAYVORA_ENV_FILE", raising=False)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    (tmp_path / ".env.local").write_text("DATABASE_URL=sqlite:///./local.db\n")

    database = reload_database_module()

    assert database.select_settings_env_file() == ".env.local"


def test_settings_env_file_uses_prod_file_for_production(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STAYVORA_ENV_FILE", raising=False)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    (tmp_path / ".env.prod").write_text("DATABASE_URL=sqlite:///./prod.db\n")

    database = reload_database_module()

    assert database.select_settings_env_file() == ".env.prod"


def test_settings_env_file_falls_back_to_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STAYVORA_ENV_FILE", raising=False)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)

    database = reload_database_module()

    assert database.select_settings_env_file() == ".env"
