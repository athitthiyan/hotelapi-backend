# Good First Issue Backlog — HotelAPI

These are ready-to-create GitHub issues for the public contributor backlog. Apply the `good first issue` label to each one.

## Add a `/health/db` Database Connectivity Endpoint

The existing `/health` endpoint confirms the API is running but does not verify database connectivity. A `/health/db` endpoint would help Railway health checks catch connection failures faster.

Acceptance criteria:
- `GET /health/db` executes a lightweight query (e.g. `SELECT 1`) and returns `200 OK` with `{"db": "ok"}`.
- Returns `503` with `{"db": "error", "detail": "..."}` if the connection fails.
- Endpoint is excluded from authentication middleware.
- Unit test covers both the healthy and unhealthy cases.

## Add Request-Level Logging Middleware

API requests currently have no structured logging. A FastAPI middleware that logs method, path, status code, and response time would make debugging Railway logs much easier.

Acceptance criteria:
- Middleware logs `method`, `path`, `status_code`, and `duration_ms` for every request.
- Uses Python's standard `logging` module (no new external dependencies).
- Log level is `INFO` for successful responses, `WARNING` for 4xx, `ERROR` for 5xx.
- Includes a test that confirms the middleware runs without breaking normal request handling.

## Standardise Error Response Format with `error_code`

Different endpoints return errors in different shapes. A consistent `{"error_code": "...", "detail": "..."}` envelope would make client error handling more robust.

Acceptance criteria:
- Define an `ErrorResponse` Pydantic model with `error_code` and `detail` fields.
- Update at least three existing error responses to use the new model.
- Document the standard error codes in a `docs/error-codes.md` file.
- Add a test that verifies the shape of the updated error responses.

## Create `.env.example` with All Variables Documented

New contributors have no reference for which environment variables are required vs optional, and what format each expects.

Acceptance criteria:
- Create `.env.example` at the repo root.
- Every variable in use across `main.py`, `database.py`, and router files is listed.
- Each variable has a one-line comment explaining its purpose and where to get the value.
- `README.md` references `.env.example` in the local setup section.

## Write Tests for Room Availability Edge Cases

The room availability query is the most business-critical endpoint but has sparse test coverage for edge cases.

Acceptance criteria:
- Test: fully booked range returns zero available rooms.
- Test: single-day booking on check-in or check-out boundary behaves correctly.
- Test: overlapping hold (not yet expired) reduces available count.
- Tests use the test database, not mocks of the ORM.
