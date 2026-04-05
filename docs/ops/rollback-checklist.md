# Stayvora Rollback Checklist

## Before deploy

- Confirm latest DB migrations applied in staging
- Capture current production app versions
- Confirm smoke credentials and seeded booking ids
- Confirm support/on-call contact for pilot window

## If customer booking flow breaks

- Freeze new deploys immediately
- Re-run production smoke suite
- Roll back frontend to last known-good build
- Roll back backend to last known-good release
- Verify `/health` and `/ops/readiness`
- Verify booking creation and active-hold recovery

## If payment/refund flow breaks

- Pause public checkout CTA if required
- Review payment incident dashboard
- Run reconciliation job
- Notify support/admin via incident email alert
- Manually resolve affected bookings before reopening checkout

## If partner inventory flow breaks

- Freeze partner inventory changes
- Revert backend release if availability math changed
- Validate partner calendar and customer blocked dates

## Exit criteria

- Smoke suite green
- No new payment mismatches
- No orphan holds
- Support queue stable
