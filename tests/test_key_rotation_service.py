"""
Tests for services/key_rotation_service.py
Covers: get_rotation_status, get_active_key, validate_key_pair,
        confirm_rotation, get_rotation_report
"""

import os
from unittest.mock import patch

from services.key_rotation_service import (
    get_rotation_status,
    get_active_key,
    validate_key_pair,
    confirm_rotation,
    get_rotation_report,
    PROVIDERS,
)


class TestGetRotationStatus:
    def test_returns_list_with_all_provider_keys(self):
        statuses = get_rotation_status()
        total_keys = sum(len(v["keys"]) for v in PROVIDERS.values())
        assert len(statuses) == total_keys

    def test_active_status_when_key_set(self):
        with patch.dict(os.environ, {"SECRET_KEY": "somevalue"}, clear=False):
            statuses = get_rotation_status()
        jwt_status = next(s for s in statuses if s.provider == "jwt:SECRET_KEY")
        assert jwt_status.status == "active"
        assert jwt_status.has_current is True
        assert jwt_status.has_next is False

    def test_rotating_status_when_next_key_set(self):
        with patch.dict(os.environ, {"SECRET_KEY": "current", "SECRET_KEY_NEXT": "next"}, clear=False):
            statuses = get_rotation_status()
        jwt_status = next(s for s in statuses if s.provider == "jwt:SECRET_KEY")
        assert jwt_status.status == "rotating"
        assert jwt_status.has_current is True
        assert jwt_status.has_next is True

    def test_missing_status_when_no_key(self):
        env = {k: "" for k in ["STRIPE_SECRET_KEY", "STRIPE_SECRET_KEY_NEXT"]}
        with patch.dict(os.environ, env, clear=False):
            statuses = get_rotation_status()
        stripe_status = next(s for s in statuses if s.provider == "stripe:STRIPE_SECRET_KEY")
        assert stripe_status.status == "missing"
        assert stripe_status.has_current is False

    def test_rotated_at_populated_when_env_set(self):
        ts = "2026-05-09T10:00:00+00:00"
        with patch.dict(os.environ, {"SECRET_KEY": "val", "SECRET_KEY_ROTATED_AT": ts}, clear=False):
            statuses = get_rotation_status()
        jwt_status = next(s for s in statuses if s.provider == "jwt:SECRET_KEY")
        assert jwt_status.rotated_at == ts

    def test_rotated_at_none_when_not_set(self):
        with patch.dict(os.environ, {"SECRET_KEY": "val"}, clear=False):
            env_without = {k: "" for k in ["SECRET_KEY_ROTATED_AT"]}
            with patch.dict(os.environ, env_without):
                statuses = get_rotation_status()
        jwt_status = next(s for s in statuses if s.provider == "jwt:SECRET_KEY")
        assert jwt_status.rotated_at is None


class TestGetActiveKey:
    def test_returns_current_key_when_no_next(self):
        with patch.dict(os.environ, {"MY_KEY": "current-value"}, clear=False):
            result = get_active_key("MY_KEY")
        assert result == "current-value"

    def test_returns_next_key_when_rotating(self):
        with patch.dict(os.environ, {"MY_KEY": "current", "MY_KEY_NEXT": "next-value"}, clear=False):
            result = get_active_key("MY_KEY")
        assert result == "next-value"

    def test_returns_empty_string_when_key_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            # Use a key name that definitely isn't in env
            result = get_active_key("NONEXISTENT_KEY_XYZ_123")
        assert result == ""


class TestValidateKeyPair:
    def test_accepts_current_key(self):
        with patch.dict(os.environ, {"TEST_KEY": "correct-key"}, clear=False):
            assert validate_key_pair("TEST_KEY", "correct-key") is True

    def test_accepts_next_key_during_rotation(self):
        with patch.dict(os.environ, {"TEST_KEY": "old", "TEST_KEY_NEXT": "new-key"}, clear=False):
            assert validate_key_pair("TEST_KEY", "new-key") is True

    def test_rejects_wrong_key(self):
        with patch.dict(os.environ, {"TEST_KEY": "correct"}, clear=False):
            assert validate_key_pair("TEST_KEY", "wrong") is False

    def test_rejects_when_no_keys_configured(self):
        env = {"TEST_KEY_EMPTY": "", "TEST_KEY_EMPTY_NEXT": ""}
        with patch.dict(os.environ, env, clear=False):
            assert validate_key_pair("TEST_KEY_EMPTY", "anything") is False


class TestConfirmRotation:
    def test_returns_true_when_next_key_present(self):
        with patch.dict(os.environ, {"MY_KEY_NEXT": "next-value"}, clear=False):
            assert confirm_rotation("MY_KEY") is True

    def test_returns_false_when_no_next_key(self):
        env = {"MY_KEY_NEXT": ""}
        with patch.dict(os.environ, env, clear=False):
            assert confirm_rotation("MY_KEY") is False

    def test_logs_warning_when_no_next_key(self, caplog):
        import logging
        env = {"MY_KEY_NEXT_NONE": ""}
        with patch.dict(os.environ, env, clear=False):
            with caplog.at_level(logging.WARNING, logger="services.key_rotation_service"):
                confirm_rotation("MY_KEY_NONE_XYZ")
        assert "No pending rotation" in caplog.text


class TestGetRotationReport:
    def test_report_has_required_fields(self):
        report = get_rotation_report()
        assert "timestamp" in report
        assert "total_keys" in report
        assert "rotating" in report
        assert "active" in report
        assert "missing" in report
        assert "keys" in report

    def test_total_keys_matches_providers(self):
        report = get_rotation_report()
        total_keys = sum(len(v["keys"]) for v in PROVIDERS.values())
        assert report["total_keys"] == total_keys

    def test_keys_list_has_correct_structure(self):
        report = get_rotation_report()
        assert len(report["keys"]) == report["total_keys"]
        for key_entry in report["keys"]:
            assert "provider" in key_entry
            assert "status" in key_entry
            assert "has_current" in key_entry
            assert "has_next" in key_entry
            assert "rotated_at" in key_entry

    def test_counts_sum_correctly(self):
        report = get_rotation_report()
        assert report["rotating"] + report["active"] + report["missing"] == report["total_keys"]

    def test_rotating_count_increases_with_next_key(self):
        with patch.dict(os.environ, {"SECRET_KEY_NEXT": "rotating-now"}, clear=False):
            report = get_rotation_report()
        assert report["rotating"] >= 1
