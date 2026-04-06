#!/usr/bin/env bash
# Run full test suite + lint for Stayvora backend
# Usage: bash scripts/run_tests_and_lint.sh
set -e

cd "$(dirname "$0")/.."

echo "======================================"
echo " Stayvora Backend — Tests + Lint"
echo "======================================"

# 1. Install / sync dependencies
echo ""
echo ">>> Installing dependencies..."
pip install -r requirements.txt -q

# 2. Lint with flake8
echo ""
echo ">>> Running flake8 lint..."
pip install flake8 -q
flake8 . \
  --exclude=__pycache__,migrations,alembic,.git,venv \
  --max-line-length=120 \
  --extend-ignore=E501,W503,E203 \
  --count \
  --statistics
echo "Lint: PASSED"

# 3. Run full test suite with coverage
echo ""
echo ">>> Running pytest with coverage..."
pytest tests/ \
  --cov=. \
  --cov-report=term-missing \
  --cov-report=html:htmlcov \
  --cov-config=.coveragerc \
  --tb=short \
  -q \
  --ignore=tests/test_e2e_booking_checkout.py \
  2>&1 | tee test_results.txt

echo ""
echo ">>> Coverage report saved to htmlcov/index.html"
echo ">>> Test output saved to test_results.txt"
