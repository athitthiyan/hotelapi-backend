# Stayvora Deployment Parity Report

| Check | Status | Detail |
| --- | --- | --- |
| Branding | FAIL | https://stayvora.co.in missing expected text: Stayvora |
| Frontend routes | FAIL | https://stayvora.co.in/search returned HTTP 404 |
| Partner portal | PASS | https://partner-portal.vercel.app/login returned HTTP 200 with 15 headers |
| Backend health | PASS | https://hotel-api-production-447d.up.railway.app/health returned HTTP 200 with 11 headers |
| Backend readiness | PASS | https://hotel-api-production-447d.up.railway.app/ready returned HTTP 200 with 11 headers |
| CORS | PASS | Access-Control-Allow-Origin=https://stayvora.co.in |
| Auth env | WARN | JWT_SECRET_KEY missing |
| Stripe env | WARN | STRIPE_SECRET_KEY missing |
| Razorpay env | WARN | RAZORPAY_KEY_ID missing |
| Database env | WARN | DATABASE_URL missing |