from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from urllib import error, request


DEFAULT_API_BASE = "https://api.stayvora.co.in"


@dataclass
class CheckResult:
    check: str
    status: str
    detail: str


def _http_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(url, data=body, headers=headers, method=method)
    with request.urlopen(req, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return response.status, json.loads(raw) if raw else {}


def _get_active_room_count(api_base: str) -> tuple[int, CheckResult]:
    try:
        _status, payload = _http_request(f"{api_base}/rooms?per_page=1")
        total = int(payload.get("total") or 0)
        if total > 0:
            return total, CheckResult("active rooms", "PASS", f"{total} active public room(s) available")
        return 0, CheckResult("active rooms", "FAIL", "No active public rooms returned by /rooms")
    except Exception as exc:  # pylint: disable=broad-except
        return 0, CheckResult("active rooms", "FAIL", f"Could not query /rooms: {exc}")


def _launch_room_payload() -> dict:
    return {
        "hotel_name": os.getenv("STAYVORA_LAUNCH_HOTEL_NAME", "Stayvora Marina Chennai"),
        "room_type": os.getenv("STAYVORA_LAUNCH_ROOM_TYPE", "suite"),
        "description": "Launch-ready Chennai suite for production booking, payment, and email smoke tests.",
        "price": float(os.getenv("STAYVORA_LAUNCH_ROOM_PRICE", "4800")),
        "original_price": float(os.getenv("STAYVORA_LAUNCH_ROOM_ORIGINAL_PRICE", "5600")),
        "total_room_count": int(os.getenv("STAYVORA_LAUNCH_ROOM_UNITS", "5")),
        "availability": True,
        "is_active": True,
        "rating": 4.7,
        "review_count": 128,
        "image_url": "https://images.unsplash.com/photo-1566073771259-6a8506099945?w=800",
        "gallery_urls": json.dumps(
            [
                "https://images.unsplash.com/photo-1566073771259-6a8506099945?w=800",
                "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?w=800",
            ]
        ),
        "amenities": json.dumps(
            [
                "Breakfast included",
                "Free WiFi",
                "Beach access",
                "Airport transfer",
                "Family friendly",
            ]
        ),
        "location": "Near Marina Beach",
        "city": "Chennai",
        "country": "India",
        "latitude": 13.0500,
        "longitude": 80.2824,
        "max_guests": 3,
        "beds": 2,
        "bathrooms": 1,
        "size_sqft": 420,
        "floor": 4,
        "is_featured": True,
    }


def _create_launch_room(api_base: str, token: str) -> tuple[int | None, list[CheckResult]]:
    results: list[CheckResult] = []
    try:
        status, payload = _http_request(
            f"{api_base}/rooms",
            method="POST",
            payload=_launch_room_payload(),
            token=token,
        )
        if status != 201:
            return None, [CheckResult("create launch room", "FAIL", f"Unexpected HTTP {status}")]
        room_id = int(payload["id"])
        results.append(CheckResult("create launch room", "PASS", f"Created room id {room_id}"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return None, [CheckResult("create launch room", "FAIL", f"HTTP {exc.code}: {detail}")]
    except Exception as exc:  # pylint: disable=broad-except
        return None, [CheckResult("create launch room", "FAIL", str(exc))]

    today = date.today()
    inventory_payload = {
        "room_id": room_id,
        "start_date": (today + timedelta(days=1)).isoformat(),
        "end_date": (today + timedelta(days=31)).isoformat(),
        "total_units": int(os.getenv("STAYVORA_LAUNCH_ROOM_UNITS", "5")),
        "available_units": int(os.getenv("STAYVORA_LAUNCH_ROOM_UNITS", "5")),
        "status": "available",
    }
    try:
        status, payload = _http_request(
            f"{api_base}/rooms/inventory",
            method="POST",
            payload=inventory_payload,
            token=token,
        )
        if status != 200:
            results.append(CheckResult("create inventory", "FAIL", f"Unexpected HTTP {status}"))
        else:
            results.append(
                CheckResult("create inventory", "PASS", f"Created {payload.get('total', 0)} inventory day(s)")
            )
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        results.append(CheckResult("create inventory", "FAIL", f"HTTP {exc.code}: {detail}"))
    except Exception as exc:  # pylint: disable=broad-except
        results.append(CheckResult("create inventory", "FAIL", str(exc)))

    return room_id, results


def main() -> None:
    api_base = os.getenv("STAYVORA_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")
    create_enabled = os.getenv("STAYVORA_CREATE_LAUNCH_ROOM", "").lower() in {"1", "true", "yes"}
    admin_token = os.getenv("STAYVORA_ADMIN_TOKEN")

    _count, first_check = _get_active_room_count(api_base)
    results = [first_check]

    if first_check.status == "FAIL":
        if not create_enabled:
            results.append(
                CheckResult(
                    "launch room creation",
                    "SKIP",
                    "Set STAYVORA_CREATE_LAUNCH_ROOM=true and STAYVORA_ADMIN_TOKEN to create one",
                )
            )
        elif not admin_token:
            results.append(CheckResult("launch room creation", "FAIL", "Missing STAYVORA_ADMIN_TOKEN"))
        else:
            _room_id, create_results = _create_launch_room(api_base, admin_token)
            results.extend(create_results)
            _count, verify_check = _get_active_room_count(api_base)
            results.append(verify_check)

    print(json.dumps([result.__dict__ for result in results], indent=2))
    if any(result.status == "FAIL" for result in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
