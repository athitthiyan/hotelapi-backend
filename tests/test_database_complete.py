"""
100% branch-coverage tests for database.py

Branches covered:
  Settings.normalize_database_url
    – postgres:// prefix → replaced with postgresql://
    – no prefix        → returned unchanged

  validate_runtime_configuration
    – app_env != "production"         → early return, no error
    – app_env == "production" + secure key (len >= 32, not default) → no error
    – app_env == "production" + insecure default key → RuntimeError
    – app_env == "production" + short key (< 32 chars, not default) → RuntimeError

  get_db  (generator)
    – happy path:  yields a session, session.close() called on exit
    – exception path: exception propagates out of with-block, close() still called
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from database import Settings, validate_runtime_configuration, get_db


# ─── Settings.normalize_database_url ─────────────────────────────────────────

class TestNormalizeDatabaseUrl:
    def test_postgres_prefix_replaced(self):
        s = Settings(database_url="postgres://user:pass@host/db")
        assert s.database_url.startswith("postgresql://")
        assert "postgres://user" not in s.database_url

    def test_postgresql_prefix_unchanged(self):
        s = Settings(database_url="postgresql://user:pass@host/db")
        assert s.database_url == "postgresql://user:pass@host/db"

    def test_sqlite_url_unchanged(self):
        s = Settings(database_url="sqlite:///test.db")
        assert s.database_url == "sqlite:///test.db"


# ─── validate_runtime_configuration ──────────────────────────────────────────

class TestValidateRuntimeConfiguration:
    def _settings_with(self, env: str, secret: str) -> Settings:
        return Settings(database_url="sqlite:///test.db", app_env=env, secret_key=secret)

    def test_non_production_returns_immediately(self):
        """app_env != 'production' → early return; even insecure key is allowed."""
        config = self._settings_with("development", "change-this-in-production")
        # Should NOT raise
        validate_runtime_configuration(config)

    def test_staging_returns_immediately(self):
        config = self._settings_with("staging", "short")
        validate_runtime_configuration(config)  # no error

    def test_production_secure_key_no_error(self):
        """Production with a long, non-default key → no error raised."""
        secure_key = "a" * 40  # 40 chars, not the default
        config = self._settings_with("production", secure_key)
        validate_runtime_configuration(config)  # should not raise

    def test_production_insecure_default_raises(self):
        """Production with the literal default key → RuntimeError."""
        config = self._settings_with("production", "change-this-in-production")
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            validate_runtime_configuration(config)

    def test_production_short_key_raises(self):
        """Production key that is not the default but is < 32 chars → RuntimeError."""
        config = self._settings_with("production", "OnlyTwentyCharsLong!")  # 20 chars
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            validate_runtime_configuration(config)

    def test_production_uppercase_env_string(self):
        """'PRODUCTION' (uppercase) is also treated as production."""
        # The check is `config.app_env.lower() != "production"`, so uppercase should
        # also trigger the validation.
        config = self._settings_with("PRODUCTION", "change-this-in-production")
        with pytest.raises(RuntimeError):
            validate_runtime_configuration(config)


# ─── get_db ───────────────────────────────────────────────────────────────────

class TestGetDb:
    def test_yields_and_closes_session(self):
        """Happy path: get_db yields a session and closes it when the generator exits."""
        mock_session = MagicMock()
        mock_session_local = MagicMock(return_value=mock_session)

        with patch("database.SessionLocal", mock_session_local):
            gen = get_db()
            session = next(gen)
            assert session is mock_session
            # Exhaust the generator (triggers the finally block)
            try:
                next(gen)
            except StopIteration:
                pass
            mock_session.close.assert_called_once()

    def test_close_called_even_when_exception_raised(self):
        """Exception propagating through get_db still closes the session."""
        mock_session = MagicMock()
        mock_session_local = MagicMock(return_value=mock_session)

        with patch("database.SessionLocal", mock_session_local):
            gen = get_db()
            next(gen)  # yield db
            with pytest.raises(RuntimeError):
                gen.throw(RuntimeError("simulated error"))
            mock_session.close.assert_called_once()
