from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, parse, request


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "reports"
    / "production_smoke_report.md"
)


@dataclass
class SmokeResult:
    flow: str
    status: str
    detail: str


def _status_icon(status: str) -> str:
    return {"PASS": "OK", "FAIL": "FAIL", "SKIP": "SKIP", "WARN": "WARN"}.get(status, status)


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, str, dict[str, str]]:
    req = request.Request(url, method=method, headers=headers or {})
    with request.urlopen(req, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")
        response_headers = {key.lower(): value for key, value in response.headers.items()}
        return response.status, body, response_headers


def _probe(flow: str, url: str, *, expected_substring: str | None = None) -> SmokeResult:
    try:
        status, body, _headers = _http_request(url)
        if status != 200:
            return SmokeResult(flow, "FAIL", f"{url} returned HTTP {status}")
        if expected_substring and expected_substring not in body:
            return SmokeResult(flow, "FAIL", f"{url} missing expected text: {expected_substring}")
        return SmokeResult(flow, "PASS", f"{url} returned HTTP 200")
    except error.HTTPError as exc:
        return SmokeResult(flow, "FAIL", f"{url} returned HTTP {exc.code}")
    except Exception as exc:  # pylint: disable=broad-except
        return SmokeResult(flow, "FAIL", f"{url} failed: {exc}")


def _probe_json(
    flow: str,
    url: str,
    validator,
) -> SmokeResult:
    try:
        status, body, _headers = _http_request(url)
        if status != 200:
            return SmokeResult(flow, "FAIL", f"{url} returned HTTP {status}")
        payload = json.loads(body)
        error_detail = validator(payload)
        if error_detail:
            return SmokeResult(flow, "FAIL", f"{url}: {error_detail}")
        return SmokeResult(flow, "PASS", f"{url} returned expected production JSON")
    except error.HTTPError as exc:
        return SmokeResult(flow, "FAIL", f"{url} returned HTTP {exc.code}")
    except json.JSONDecodeError as exc:
        return SmokeResult(flow, "FAIL", f"{url} returned invalid JSON: {exc}")
    except Exception as exc:  # pylint: disable=broad-except
        return SmokeResult(flow, "FAIL", f"{url} failed: {exc}")


def _discover_room_id(api_base: str) -> tuple[str | None, SmokeResult]:
    try:
        status, body, _headers = _http_request(f"{api_base}/rooms?per_page=1")
        if status != 200:
            return None, SmokeResult(
                "room discovery",
                "WARN",
                f"Skipping room-specific API checks; /rooms returned HTTP {status}",
            )
        payload = json.loads(body)
        rooms = payload.get("rooms") or []
        if not rooms:
            return None, SmokeResult(
                "room discovery",
                "WARN",
                "Skipping room-specific API checks; /rooms returned no active rooms",
            )
        return str(rooms[0]["id"]), SmokeResult(
            "room discovery",
            "PASS",
            f"Using live room id {rooms[0]['id']}",
        )
    except Exception as exc:  # pylint: disable=broad-except
        return None, SmokeResult(
            "room discovery",
            "WARN",
            f"Skipping room-specific API checks; discovery failed: {exc}",
        )


def _probe_with_auth(flow: str, url: str, token: str | None) -> SmokeResult:
    if not token:
        return SmokeResult(flow, "SKIP", "Missing auth token in environment")
    try:
        status, _body, _headers = _http_request(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if status in {200, 204}:
            return SmokeResult(flow, "PASS", f"{url} returned HTTP {status}")
        return SmokeResult(flow, "FAIL", f"{url} returned HTTP {status}")
    except error.HTTPError as exc:
        return SmokeResult(flow, "FAIL", f"{url} returned HTTP {exc.code}")
    except Exception as exc:  # pylint: disable=broad-except
        return SmokeResult(flow, "FAIL", f"{url} failed: {exc}")


def _validate_health(payload: dict) -> str | None:
    checks = payload.get("checks", {})
    database = checks.get("database", {})
    scheduler = checks.get("scheduler", {})
    redis = checks.get("redis", {})
    notification_queue = checks.get("notification_queue", {})

    if payload.get("status") != "healthy":
        return f"status is {payload.get('status')!r}, expected 'healthy'"
    if payload.get("environment") != "production":
        return f"environment is {payload.get('environment')!r}, expected 'production'"
    if database.get("status") != "connected":
        return f"database status is {database.get('status')!r}"
    if scheduler.get("status") != "running":
        return f"scheduler status is {scheduler.get('status')!r}"
    if not redis.get("configured"):
        return "redis is not configured"
    if not redis.get("connected") or redis.get("mode") != "redis":
        return f"redis is not connected: {redis}"
    if notification_queue.get("pending") is None:
        return f"notification queue is not reporting pending count: {notification_queue}"
    return None


def _validate_ready(payload: dict) -> str | None:
    if payload.get("status") != "ready":
        return f"status is {payload.get('status')!r}, expected 'ready'"
    if payload.get("database") != "connected":
        return f"database is {payload.get('database')!r}"
    for key in ("pending_notifications", "processing_payments"):
        if key not in payload:
            return f"missing operational count {key!r}"
    return None


def _validate_deep_health(payload: dict) -> str | None:
    checks = payload.get("checks", {})
    database = checks.get("database", {})
    redis = checks.get("redis", {})
    email = checks.get("email", {})
    payments = checks.get("payments", {})

    if payload.get("status") != "healthy":
        return f"status is {payload.get('status')!r}, expected 'healthy'"
    if database.get("status") != "healthy":
        return f"database status is {database.get('status')!r}"
    if not redis.get("connected") or redis.get("mode") != "redis":
        return f"redis is not connected: {redis}"
    if not email.get("configured"):
        return "Resend email is not configured"
    if not payments.get("razorpay", {}).get("configured"):
        return "Razorpay is not configured"
    if payments.get("stripe", {}).get("enabled") and not payments.get("stripe", {}).get("configured"):
        return "Stripe is enabled but not configured"
    return None


def _build_results() -> list[SmokeResult]:
    web_base = os.getenv("STAYVORA_WEB_BASE_URL", "https://stayvora.co.in").rstrip("/")
    api_base = os.getenv("STAYVORA_API_BASE_URL", "https://api.stayvora.co.in").rstrip("/")
    partner_base = os.getenv("STAYVORA_PARTNER_BASE_URL", "https://partner.stayvora.co.in").rstrip("/")
    configured_room_id = os.getenv("STAYVORA_SMOKE_ROOM_ID")
    booking_id = os.getenv("STAYVORA_SMOKE_BOOKING_ID")
    customer_token = os.getenv("STAYVORA_SMOKE_CUSTOMER_TOKEN")
    partner_token = os.getenv("STAYVORA_SMOKE_PARTNER_TOKEN")
    admin_token = os.getenv("STAYVORA_SMOKE_ADMIN_TOKEN")
    default_from = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    default_to = (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()
    today = os.getenv("STAYVORA_SMOKE_FROM_DATE", default_from)
    to_date = os.getenv("STAYVORA_SMOKE_TO_DATE", default_to)

    if configured_room_id:
        room_id = configured_room_id
        room_discovery = SmokeResult("room discovery", "PASS", f"Using configured room id {room_id}")
    else:
        room_id, room_discovery = _discover_room_id(api_base)

    results = [
        room_discovery,
        _probe("homepage load", web_base, expected_substring="Stayvora"),
        _probe("search page", f"{web_base}/search"),
        _probe("partner portal load", f"{partner_base}/login"),
        _probe_json("backend health gate", f"{api_base}/health", _validate_health),
        _probe_json("backend readiness gate", f"{api_base}/ready", _validate_ready),
        _probe_json("backend dependency gate", f"{api_base}/health/deep", _validate_deep_health),
    ]
    if room_id:
        blocked_dates_url = (
            f"{api_base}/rooms/{room_id}/unavailable-dates?"
            + parse.urlencode({"from_date": today, "to_date": to_date})
        )
        results.extend(
            [
                _probe("room detail", f"{web_base}/rooms/{room_id}"),
                _probe("blocked dates API", blocked_dates_url),
            ]
        )
    else:
        results.extend(
            [
                SmokeResult("room detail", "SKIP", "No active API room discovered"),
                SmokeResult("blocked dates API", "SKIP", "No active API room discovered"),
            ]
        )

    results.append(
        _probe_with_auth("active booking CTA", f"{api_base}/bookings/active-hold", customer_token)
    )
    results.append(
        _probe_with_auth(
            "partner inventory update surface",
            f"{api_base}/partner/calendar?room_type_id={room_id or 0}",
            partner_token,
        )
    )

    if booking_id:
        results.append(
            _probe_with_auth("invoice download", f"{api_base}/bookings/{booking_id}/invoice", customer_token)
        )
        results.append(
            _probe_with_auth("voucher download", f"{api_base}/bookings/{booking_id}/voucher", customer_token)
        )
        results.append(
            _probe_with_auth("refund timeline", f"{api_base}/payments/refunds/{booking_id}", admin_token or customer_token)
        )
    else:
        results.extend(
            [
                SmokeResult("invoice download", "SKIP", "Missing STAYVORA_SMOKE_BOOKING_ID"),
                SmokeResult("voucher download", "SKIP", "Missing STAYVORA_SMOKE_BOOKING_ID"),
                SmokeResult("refund timeline", "SKIP", "Missing STAYVORA_SMOKE_BOOKING_ID"),
            ]
        )

    results.extend(
        [
            SmokeResult("login", "SKIP", "Manual credential flow required in pilot environment"),
            SmokeResult("hold creation", "SKIP", "Requires seeded availability and login credentials"),
            SmokeResult("payment success", "SKIP", "Requires live/sandbox card or UPI credentials"),
            SmokeResult("payment failure + retry", "SKIP", "Requires gateway test data and seeded booking"),
            SmokeResult("cancellation", "SKIP", "Requires reversible seeded booking"),
            SmokeResult("admin refund override", "SKIP", "Requires admin token plus seeded refundable booking"),
        ]
    )
    return results


def _write_report(results: list[SmokeResult]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Stayvora Production Smoke Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "| Flow | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for result in results:
        lines.append(f"| {result.flow} | {_status_icon(result.status)} | {result.detail} |")
    summary = {
        "pass": sum(1 for result in results if result.status == "PASS"),
        "fail": sum(1 for result in results if result.status == "FAIL"),
        "skip": sum(1 for result in results if result.status == "SKIP"),
        "warn": sum(1 for result in results if result.status == "WARN"),
    }
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- PASS: {summary['pass']}",
            f"- FAIL: {summary['fail']}",
            f"- WARN: {summary['warn']}",
            f"- SKIP: {summary['skip']}",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    results = _build_results()
    _write_report(results)
    print(json.dumps([result.__dict__ for result in results], indent=2))
    if any(result.status == "FAIL" for result in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
