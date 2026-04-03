from unittest.mock import MagicMock, patch
import builtins

from sqlalchemy.exc import SQLAlchemyError

import main


def test_root_endpoint():
    assert main.root() == {
        "status": "HotelAPI is running",
        "version": "1.0.0",
        "docs": "/docs",
    }


def test_health_check_connected():
    connection = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = connection
    manager.__exit__.return_value = False

    with patch.object(main.engine, "connect", return_value=manager):
        body = main.health_check()

    assert body["database"] == "connected"


def test_health_check_unavailable():
    with patch.object(main.engine, "connect", side_effect=SQLAlchemyError("boom")):
        body = main.health_check()

    assert body["database"] == "unavailable"


def test_startup_checks_success():
    connection = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = connection
    manager.__exit__.return_value = False

    inspector = MagicMock()
    inspector.get_table_names.return_value = ["rooms", "bookings", "transactions", "users"]

    with patch.object(main.engine, "begin", return_value=manager), patch.object(
        main, "inspect", return_value=inspector
    ), patch.object(main.logger, "info") as logger_info:
        main.startup_checks()

    connection.execute.assert_called_once()
    logger_info.assert_called_once()


def test_startup_checks_warns_when_tables_are_missing():
    connection = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = connection
    manager.__exit__.return_value = False
    inspector = MagicMock()
    inspector.get_table_names.return_value = ["rooms"]

    with patch.object(main.engine, "begin", return_value=manager), patch.object(
        main, "inspect", return_value=inspector
    ), patch.object(main.logger, "warning") as logger_warning:
        main.startup_checks()

    logger_warning.assert_called_once()


def test_startup_checks_auto_creates_schema_when_enabled():
    connection = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = connection
    manager.__exit__.return_value = False
    inspector = MagicMock()
    inspector.get_table_names.return_value = []

    with patch.object(main.engine, "begin", return_value=manager), patch.object(
        main, "inspect", return_value=inspector
    ), patch.object(main, "settings") as settings, patch.object(
        main.Base.metadata, "create_all"
    ) as create_all:
        settings.auto_create_schema = True
        main.startup_checks()

    create_all.assert_called_once_with(bind=connection)


def test_startup_checks_handles_database_error():
    with patch.object(main.engine, "begin", side_effect=SQLAlchemyError("boom")), patch.object(
        main.logger, "exception"
    ) as logger_exception:
        main.startup_checks()

    logger_exception.assert_called_once()


def test_seed_database_when_already_seeded():
    session = MagicMock()
    room_query = MagicMock()
    room_query.count.return_value = 2
    session.query.return_value = room_query

    with patch.object(main, "SessionLocal", return_value=session):
        response = main.seed_database()

    assert response == {"message": "Database already seeded"}
    session.close.assert_called_once()


def test_seed_database_seeds_rooms():
    session = MagicMock()
    room_query = MagicMock()
    room_query.count.return_value = 0
    session.query.return_value = room_query

    fake_models = __import__("models")
    with patch.object(main, "SessionLocal", return_value=session), patch.object(
        builtins, "__import__", return_value=fake_models
    ):
        response = main.seed_database()

    assert response == {"message": "Seeded 6 rooms successfully"}
    assert session.add.call_count == 6
    session.commit.assert_called_once()
    session.close.assert_called_once()
