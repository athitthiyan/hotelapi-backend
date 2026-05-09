"""
Tests for:
  - services/otp_service._send_sms_fast2sms
  - services/otp_service._send_sms_twilio
  - POST /auth/set-password endpoint
  - models.User.has_password property
  - schemas.SetPasswordRequest validator
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from pydantic import ValidationError

import schemas
from services.otp_service import _send_sms_fast2sms, _send_sms_twilio


# --- Schema: SetPasswordRequest ------------------------------------------

class TestSetPasswordRequestSchema:
    def test_valid_password_accepted(self):
        req = schemas.SetPasswordRequest(new_password="ValidPass1")
        assert req.new_password == "ValidPass1"

    def test_too_short_rejected(self):
        with pytest.raises(ValidationError):
            schemas.SetPasswordRequest(new_password="Short1A")

    def test_all_lowercase_rejected(self):
        with pytest.raises(ValidationError):
            schemas.SetPasswordRequest(new_password="weakpassword1")

    def test_no_digit_rejected(self):
        with pytest.raises(ValidationError):
            schemas.SetPasswordRequest(new_password="WeakPassOnly")

    def test_no_uppercase_rejected(self):
        with pytest.raises(ValidationError):
            schemas.SetPasswordRequest(new_password="weakpass1234")

    def test_no_lowercase_rejected(self):
        with pytest.raises(ValidationError):
            schemas.SetPasswordRequest(new_password="UPPERCASE1234")


# --- Schema: ChangePasswordRequest strength validator --------------------

class TestChangePasswordRequestStrength:
    def test_valid_password_accepted(self):
        req = schemas.ChangePasswordRequest(current_password="old", new_password="NewPass123!")
        assert req.new_password == "NewPass123!"

    def test_all_lowercase_rejected(self):
        with pytest.raises(ValidationError):
            schemas.ChangePasswordRequest(current_password="old", new_password="weakpassxx")

    def test_no_digit_rejected(self):
        with pytest.raises(ValidationError):
            schemas.ChangePasswordRequest(current_password="old", new_password="WeakPassOnly")

    def test_no_uppercase_rejected(self):
        with pytest.raises(ValidationError):
            schemas.ChangePasswordRequest(current_password="old", new_password="weakpass1234")


# --- models.User.has_password property -----------------------------------

class TestUserHasPasswordProperty:
    """Test User.has_password property logic via fget to avoid SQLAlchemy instrumentation."""

    @staticmethod
    def _fget(hashed_password):
        import models
        from types import SimpleNamespace
        ns = SimpleNamespace(hashed_password=hashed_password)
        return models.User.has_password.fget(ns)

    def test_has_password_true_when_hash_set(self):
        assert self._fget("$pbkdf2-sha256$...") is True

    def test_has_password_false_when_null(self):
        assert self._fget(None) is False

    def test_has_password_false_when_empty_string(self):
        assert self._fget("") is False


# --- _send_sms_fast2sms --------------------------------------------------

class TestSendSmsF2S:
    def test_returns_false_when_no_api_key(self):
        with patch("database.settings") as mock_settings:
            mock_settings.fast2sms_api_key = ""
            result = _send_sms_fast2sms("+919876543210", "123456")
        assert result is False

    def test_returns_false_for_unparseable_number(self):
        with patch("database.settings") as mock_settings:
            mock_settings.fast2sms_api_key = "somekey"
            result = _send_sms_fast2sms("notanumber", "123456")
        assert result is False

    def test_strips_91_prefix_from_12_digit_number(self):
        with patch("database.settings") as mock_settings, \
             patch("httpx.post") as mock_post:
            mock_settings.fast2sms_api_key = "somekey"
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"return": True}
            mock_post.return_value = mock_resp
            result = _send_sms_fast2sms("+919876543210", "123456")
        assert result is True
        call_json = mock_post.call_args.kwargs["json"]
        assert call_json["numbers"] == "9876543210"

    def test_returns_true_on_success_response(self):
        with patch("database.settings") as mock_settings, \
             patch("httpx.post") as mock_post:
            mock_settings.fast2sms_api_key = "somekey"
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"return": True}
            mock_post.return_value = mock_resp
            result = _send_sms_fast2sms("9876543210", "123456")
        assert result is True

    def test_returns_false_when_api_returns_error(self):
        with patch("database.settings") as mock_settings, \
             patch("httpx.post") as mock_post:
            mock_settings.fast2sms_api_key = "somekey"
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"return": False, "message": "error"}
            mock_post.return_value = mock_resp
            result = _send_sms_fast2sms("9876543210", "123456")
        assert result is False

    def test_returns_false_on_http_exception(self):
        with patch("database.settings") as mock_settings, \
             patch("httpx.post", side_effect=Exception("network error")):
            mock_settings.fast2sms_api_key = "somekey"
            result = _send_sms_fast2sms("9876543210", "123456")
        assert result is False


# --- _send_sms_twilio ----------------------------------------------------

class TestSendSmsTwilio:
    def test_returns_false_when_no_credentials(self):
        with patch("database.settings") as mock_settings:
            mock_settings.twilio_account_sid = ""
            mock_settings.twilio_auth_token = ""
            mock_settings.twilio_from_number = ""
            result = _send_sms_twilio("+919876543210", "123456")
        assert result is False

    def test_returns_false_when_partial_credentials(self):
        with patch("database.settings") as mock_settings:
            mock_settings.twilio_account_sid = "ACxxx"
            mock_settings.twilio_auth_token = ""
            mock_settings.twilio_from_number = "+1234567890"
            result = _send_sms_twilio("+919876543210", "123456")
        assert result is False

    def test_returns_true_on_successful_send(self):
        with patch("database.settings") as mock_settings, \
             patch("twilio.rest.Client") as mock_client_cls:
            mock_settings.twilio_account_sid = "ACxxx"
            mock_settings.twilio_auth_token = "token"
            mock_settings.twilio_from_number = "+1234567890"
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_message = MagicMock()
            mock_message.sid = "SMxxx"
            mock_client.messages.create.return_value = mock_message
            result = _send_sms_twilio("+919876543210", "123456")
        assert result is True

    def test_returns_false_on_twilio_exception(self):
        with patch("database.settings") as mock_settings, \
             patch("twilio.rest.Client", side_effect=Exception("twilio error")):
            mock_settings.twilio_account_sid = "ACxxx"
            mock_settings.twilio_auth_token = "token"
            mock_settings.twilio_from_number = "+1234567890"
            result = _send_sms_twilio("+919876543210", "123456")
        assert result is False


# --- POST /auth/set-password ---------------------------------------------

@pytest.fixture()
def client():
    from main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def db_session():
    from database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def _make_user(db, *, hashed_password=None, is_admin=False):
    import models
    from routers.auth import hash_password
    user = models.User(
        email="test@example.com",
        full_name="Test User",
        hashed_password=hashed_password,
        is_admin=is_admin,
        is_partner=False,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _token_for(user):
    from routers.auth import create_token
    from datetime import timedelta
    return create_token(user, "access", timedelta(minutes=30))


class TestSetPasswordEndpoint:
    def test_sso_user_can_set_password(self, client, db_session):
        from main import app
        from database import get_db
        user = _make_user(db_session, hashed_password=None)
        token = _token_for(user)

        app.dependency_overrides[get_db] = lambda: db_session
        resp = client.post(
            "/auth/set-password",
            json={"new_password": "NewSecure123!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert "set successfully" in resp.json()["message"].lower()
        db_session.refresh(user)
        assert user.hashed_password is not None

    def test_user_with_password_cannot_use_set_password(self, client, db_session):
        from main import app
        from database import get_db
        from routers.auth import hash_password
        user = _make_user(db_session, hashed_password=hash_password("OldPass123!"))
        token = _token_for(user)

        app.dependency_overrides[get_db] = lambda: db_session
        resp = client.post(
            "/auth/set-password",
            json={"new_password": "NewSecure123!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.dependency_overrides.clear()

        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "password_already_set"

    def test_set_password_requires_auth(self, client):
        resp = client.post("/auth/set-password", json={"new_password": "NewSecure123!"})
        assert resp.status_code == 401

    def test_set_password_validates_strength(self, client, db_session):
        from main import app
        from database import get_db
        user = _make_user(db_session, hashed_password=None)
        token = _token_for(user)

        app.dependency_overrides[get_db] = lambda: db_session
        resp = client.post(
            "/auth/set-password",
            json={"new_password": "weakpassword"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.dependency_overrides.clear()

        assert resp.status_code == 422

    def test_change_password_returns_400_for_sso_user(self, client, db_session):
        from main import app
        from database import get_db
        user = _make_user(db_session, hashed_password=None)
        token = _token_for(user)

        app.dependency_overrides[get_db] = lambda: db_session
        resp = client.post(
            "/auth/change-password",
            json={"current_password": "anything", "new_password": "NewSecure123!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.dependency_overrides.clear()

        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "no_password_set"
