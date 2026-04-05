from unittest.mock import MagicMock, patch

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
        state = MagicMock()
        main.startup_checks(state)

    connection.execute.assert_called_once()
    logger_info.assert_called_once()
    assert hasattr(state, "hold_expiry_scheduler")


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
        main.startup_checks(MagicMock())

    assert logger_warning.call_count >= 1
    warning_messages = [call.args[0] for call in logger_warning.call_args_list]
    assert any("Database schema is missing tables" in message for message in warning_messages)


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
        main.startup_checks(MagicMock())

    create_all.assert_called_once_with(bind=connection)


def test_startup_checks_handles_database_error():
    with patch.object(main.engine, "begin", side_effect=SQLAlchemyError("boom")), patch.object(
        main.logger, "exception"
    ) as logger_exception:
        main.startup_checks(MagicMock())

    logger_exception.assert_called_once()


def test_startup_checks_handles_runtime_configuration_error():
    with patch.object(
        main, "validate_runtime_configuration", side_effect=RuntimeError("bad secret")
    ), patch.object(main.logger, "exception") as logger_exception:
        main.startup_checks(MagicMock())

    logger_exception.assert_called_once()


def test_startup_checks_disables_scheduler_when_dependency_missing():
    state = MagicMock()

    with patch.object(main, "BackgroundScheduler", None), patch.object(
        main.engine, "begin", side_effect=SQLAlchemyError("boom")
    ), patch.object(main.logger, "warning") as logger_warning:
        main.startup_checks(state)

    assert state.hold_expiry_scheduler is None
    logger_warning.assert_called_once()


def test_shutdown_scheduler_stops_existing_scheduler():
    scheduler = MagicMock()
    state = MagicMock(hold_expiry_scheduler=scheduler)

    main.shutdown_scheduler(state)

    scheduler.shutdown.assert_called_once_with(wait=False)


def test_seed_database_when_already_seeded():
    session = MagicMock()
    room_query = MagicMock()
    room_query.count.return_value = 2
    user_query = MagicMock()
    existing_user = MagicMock()
    existing_user.id = 1
    user_query.filter.return_value.first.return_value = existing_user
    hotel_query = MagicMock()
    existing_hotel = MagicMock()
    existing_hotel.id = 10
    hotel_query.filter.return_value.first.return_value = existing_hotel
    partner_room_query = MagicMock()
    partner_room_query.filter.return_value.first.return_value = object()
    session.query.side_effect = [room_query, user_query, user_query, hotel_query, partner_room_query]

    with patch.object(main, "SessionLocal", return_value=session):
        response = main.seed_database()

    assert response["message"] == "Seed completed successfully"
    assert response["rooms_created"] == 0
    assert response["admin_created"] is False
    assert response["partner_created"] is False
    session.close.assert_called_once()


def test_seed_database_seeds_rooms():
    session = MagicMock()
    room_query = MagicMock()
    room_query.count.return_value = 0
    missing_query = MagicMock()
    missing_query.filter.return_value.first.return_value = None
    session.query.side_effect = [room_query, missing_query, missing_query, missing_query, missing_query]

    with patch.object(main, "SessionLocal", return_value=session):
        response = main.seed_database()

    assert response["message"] == "Seed completed successfully"
    assert response["rooms_created"] == 6
    assert response["admin_created"] is True
    assert response["partner_created"] is True
    assert response["partner_hotel_created"] is True
    assert response["partner_room_created"] is True
    assert session.add.call_count == 10
    session.commit.assert_called_once()
    session.close.assert_called_once()
