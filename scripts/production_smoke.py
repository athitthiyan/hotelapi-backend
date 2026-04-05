from __future__ import annotations

import json
import os
from dataclasses import dataclass
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


def _build_results() -> list[SmokeResult]:
    web_base = os.getenv("STAYVORA_WEB_BASE_URL", "https://stayvora.co.in").rstrip("/")
    api_base = os.getenv("STAYVORA_API_BASE_URL", "https://hotel-api-production-447d.up.railway.app").rstrip("/")
    partner_base = os.getenv("STAYVORA_PARTNER_BASE_URL", "https://partner-portal.vercel.app").rstrip("/")
    room_id = os.getenv("STAYVORA_SMOKE_ROOM_ID", "1")
    booking_id = os.getenv("STAYVORA_SMOKE_BOOKING_ID")
    customer_token = os.getenv("STAYVORA_SMOKE_CUSTOMER_TOKEN")
    partner_token = os.getenv("STAYVORA_SMOKE_PARTNER_TOKEN")
    admin_token = os.getenv("STAYVORA_SMOKE_ADMIN_TOKEN")
    today = os.getenv("STAYVORA_SMOKE_FROM_DATE", "2026-04-10")
    to_date = os.getenv("STAYVORA_SMOKE_TO_DATE", "2026-04-12")

    blocked_dates_url = (
        f"{api_base}/rooms/{room_id}/unavailable-dates?"
        + parse.urlencode({"from_date": today, "to_date": to_date})
    )
    results = [
        _probe("homepage load", web_base, expected_substring="Stayvora"),
        _probe("search page", f"{web_base}/search"),
        _probe("room detail", f"{web_base}/rooms/{room_id}"),
        _probe("blocked dates API", blocked_dates_url),
        _probe("partner portal load", f"{partner_base}/login"),
        _probe("backend health", f"{api_base}/health"),
    ]

    results.append(
        _probe_with_auth("active booking CTA", f"{api_base}/bookings/active-hold", customer_token)
    )
    results.append(
        _probe_with_auth("partner inventory update surface", f"{api_base}/partner/calendar?room_type_id={room_id}", partner_token)
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
        "| Flow | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for result in results:
        lines.append(f"| {result.flow} | {result.status} | {result.detail} |")
    summary = {
        "pass": sum(1 for result in results if result.status == "PASS"),
        "fail": sum(1 for result in results if result.status == "FAIL"),
        "skip": sum(1 for result in results if result.status == "SKIP"),
    }
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- PASS: {summary['pass']}",
            f"- FAIL: {summary['fail']}",
            f"- SKIP: {summary['skip']}",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    results = _build_results()
    _write_report(results)
    print(json.dumps([result.__dict__ for result in results], indent=2))


if __name__ == "__main__":
    main()
