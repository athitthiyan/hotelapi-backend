# Stayvora Production Smoke Report

| Flow | Status | Detail |
| --- | --- | --- |
| homepage load | FAIL | https://stayvora.co.in missing expected text: Stayvora |
| search page | FAIL | https://stayvora.co.in/search returned HTTP 404 |
| room detail | FAIL | https://stayvora.co.in/rooms/1 returned HTTP 404 |
| blocked dates API | FAIL | https://hotel-api-production-447d.up.railway.app/rooms/1/unavailable-dates?from_date=2026-04-10&to_date=2026-04-12 returned HTTP 500 |
| partner portal load | PASS | https://partner-portal.vercel.app/login returned HTTP 200 |
| backend health | PASS | https://hotel-api-production-447d.up.railway.app/health returned HTTP 200 |
| active booking CTA | SKIP | Missing auth token in environment |
| partner inventory update surface | SKIP | Missing auth token in environment |
| invoice download | SKIP | Missing STAYVORA_SMOKE_BOOKING_ID |
| voucher download | SKIP | Missing STAYVORA_SMOKE_BOOKING_ID |
| refund timeline | SKIP | Missing STAYVORA_SMOKE_BOOKING_ID |
| login | SKIP | Manual credential flow required in pilot environment |
| hold creation | SKIP | Requires seeded availability and login credentials |
| payment success | SKIP | Requires live/sandbox card or UPI credentials |
| payment failure + retry | SKIP | Requires gateway test data and seeded booking |
| cancellation | SKIP | Requires reversible seeded booking |
| admin refund override | SKIP | Requires admin token plus seeded refundable booking |

## Summary

- PASS: 2
- FAIL: 4
- SKIP: 11