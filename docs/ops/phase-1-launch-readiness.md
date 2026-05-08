# Stayvora Phase 1 Launch Readiness

Phase 1 is about proving that the product can survive real users. Do not treat a deploy as ready until the release gates below pass.

## Current Baseline

- API domain: `https://api.stayvora.co.in`
- Runtime environment: `production`
- Database: Supabase PostgreSQL
- Cache: Railway Redis through `REDIS_URL`
- Scheduler: APScheduler hold release and notification processor
- Deployment gate: `alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port $PORT`

## Release Gates

Run these after every production deploy:

```powershell
python scripts/production_smoke.py
python scripts/deployment_parity_check.py
```

Required live API results:

- `/health` returns `status=healthy`
- `/health` returns `environment=production`
- `/health` shows database `connected`
- `/health` shows Redis `configured=true`, `connected=true`, `mode=redis`
- `/health` shows scheduler `running`
- `/ready` returns `status=ready`
- `/health/deep` confirms Resend and payment gateway configuration

Reports are written to:

- `reports/production_smoke_report.md`
- `reports/production_parity_report.md`

## Phase 1 Work Queue

1. Payment verification
   - Razorpay order creation
   - Razorpay captured webhook
   - Razorpay failed webhook
   - Duplicate webhook idempotency
   - Refund lifecycle
   - Stripe flow only if Stripe remains enabled

2. Email verification
   - Booking hold email
   - Booking confirmation email
   - GST invoice PDF attachment
   - Refund email
   - Password reset email

3. Observability
   - Add Sentry SDK
   - Set `SENTRY_DSN`
   - Trigger one controlled test exception
   - Add uptime monitoring for `/health`

4. Security cleanup
   - Rotate exposed API keys and secrets
   - Confirm `/docs` and `/redoc` are disabled in production
   - Add secret scanning in CI
   - Keep CORS restricted to production frontend domains

5. Frontend parity
   - Customer app uses `https://api.stayvora.co.in`
   - Payment app uses `https://api.stayvora.co.in`
   - Admin app uses `https://api.stayvora.co.in`
   - Partner app uses `https://api.stayvora.co.in`
   - Production domains are present in `ALLOWED_ORIGINS`

## Exit Criteria

Phase 1 is complete when:

- Production smoke script passes with zero failures
- Deployment parity script passes with zero failures
- Payment sandbox smoke passes end to end
- Email delivery smoke passes end to end
- Monitoring alerts are configured and tested
- Secrets have been rotated after setup exposure
- All production frontends use final domains and final API base URL
