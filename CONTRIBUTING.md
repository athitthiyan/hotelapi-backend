# Contributing to HotelAPI

Thanks for helping improve HotelAPI. This project welcomes bug reports, feature ideas, documentation fixes, and tests.

## Ways to Contribute

- Pick an issue labeled `good first issue` or `help wanted`.
- Report a bug with expected behavior, actual behavior, and reproduction steps (include a `curl` command or request payload where useful).
- Suggest a feature by describing the platform use case it enables.
- Improve test coverage, documentation, or error messages.

## Local Setup

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
alembic upgrade head
uvicorn main:app --reload --port 8000
```

API docs at [http://localhost:8000/docs](http://localhost:8000/docs).

## Before Opening a Pull Request

```bash
# Run tests
pytest

# Check types (if mypy is configured)
mypy .
```

## Pull Request Guidelines

- Keep PRs focused on one behavior or endpoint.
- Add or update tests when behavior changes.
- Include or update an Alembic migration for any schema changes.
- Describe any new environment variables required.

## Good First Issue Ideas

- Add request-level logging middleware for easier debugging.
- Improve error response format to include a machine-readable `error_code` field.
- Write tests for the room availability edge cases (fully booked date ranges).
- Add a `GET /health/db` endpoint that verifies database connectivity.
- Document the `.env.example` file with all required and optional variables.
