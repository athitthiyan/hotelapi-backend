from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "reports"
    / "production_parity_report.md"
)


@dataclass
class ParityCheck:
    check: str
    status: str
    detail: str


def _http_get(url: str) -> tuple[int, str, dict[str, str]]:
    req = request.Request(url, method="GET")
    with request.urlopen(req, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")
        response_headers = {key.lower(): value for key, value in response.headers.items()}
        return response.status, body, response_headers


def _check_url(name: str, url: str, expected_text: str | None = None) -> ParityCheck:
    try:
        status, body, headers = _http_get(url)
        if status != 200:
            return ParityCheck(name, "FAIL", f"{url} returned HTTP {status}")
        if expected_text and expected_text not in body:
            return ParityCheck(name, "FAIL", f"{url} missing expected text: {expected_text}")
        return ParityCheck(name, "PASS", f"{url} returned HTTP 200 with {len(headers)} headers")
    except error.HTTPError as exc:
        return ParityCheck(name, "FAIL", f"{url} returned HTTP {exc.code}")
    except Exception as exc:  # pylint: disable=broad-except
        return ParityCheck(name, "FAIL", f"{url} failed: {exc}")


def _check_env(name: str, key: str) -> ParityCheck:
    return ParityCheck(name, "PASS" if os.getenv(key) else "WARN", f"{key} {'present' if os.getenv(key) else 'missing'}")


def _check_cors(api_base: str, origin: str) -> ParityCheck:
    req = request.Request(
        f"{api_base}/health",
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            allow_origin = response.headers.get("Access-Control-Allow-Origin")
            if allow_origin in {origin, "*"}:
                return ParityCheck("CORS", "PASS", f"Access-Control-Allow-Origin={allow_origin}")
            return ParityCheck("CORS", "FAIL", f"Unexpected CORS header: {allow_origin}")
    except error.HTTPError as exc:
        return ParityCheck("CORS", "FAIL", f"Preflight failed with HTTP {exc.code}")
    except Exception as exc:  # pylint: disable=broad-except
        return ParityCheck("CORS", "FAIL", f"Preflight failed: {exc}")


def _build_checks() -> list[ParityCheck]:
    web_base = os.getenv("STAYVORA_WEB_BASE_URL", "https://stayvora.co.in").rstrip("/")
    api_base = os.getenv("STAYVORA_API_BASE_URL", "https://hotel-api-production-447d.up.railway.app").rstrip("/")
    partner_base = os.getenv("STAYVORA_PARTNER_BASE_URL", "https://partner-portal.vercel.app").rstrip("/")
    return [
        _check_url("Branding", web_base, "Stayvora"),
        _check_url("Frontend routes", f"{web_base}/search"),
        _check_url("Partner portal", f"{partner_base}/login"),
        _check_url("Backend health", f"{api_base}/health"),
        _check_url("Backend readiness", f"{api_base}/ready"),
        _check_cors(api_base, web_base),
        _check_env("Auth env", "JWT_SECRET_KEY"),
        _check_env("Stripe env", "STRIPE_SECRET_KEY"),
        _check_env("Razorpay env", "RAZORPAY_KEY_ID"),
        _check_env("Database env", "DATABASE_URL"),
    ]


def _write_report(results: list[ParityCheck]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Stayvora Deployment Parity Report",
        "",
        "| Check | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for result in results:
        lines.append(f"| {result.check} | {result.status} | {result.detail} |")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    results = _build_checks()
    _write_report(results)
    print(json.dumps([result.__dict__ for result in results], indent=2))


if __name__ == "__main__":
    main()
