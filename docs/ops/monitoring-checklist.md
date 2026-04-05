# Stayvora Monitoring Checklist

## Uptime

- Frontend uptime probe for `https://stayvora.co.in`
- API uptime probe for `/health`
- Partner portal uptime probe for `/login`
- Alert destination: `SUPPORT_ALERT_EMAIL`

## Payments

- Alert on webhook failure queue growth
- Alert on reconciliation mismatches
- Alert on refund failure state transitions
- Alert on stale processing payments

## Database

- Verify managed Postgres daily backups
- Verify point-in-time restore retention
- Record latest backup proof before pilot launch

## Customer Journey

- Monitor `bookings/active-hold` failure rate
- Monitor booking creation 4xx/5xx rate
- Monitor invoice/voucher download 4xx/5xx rate

## Partner Journey

- Monitor `/partner/calendar` errors
- Monitor `/partner/payouts` errors
- Monitor support ticket queue growth
