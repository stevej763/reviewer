#!/bin/sh
# Run the test suite inside the app image (no local Python setup needed).
set -e
cd "$(dirname "$0")/.."
docker compose build -q reviewer
docker run --rm -u root -v "$PWD:/src:ro" -w /src -e PYTHONDONTWRITEBYTECODE=1 reviewer:latest \
  sh -c "pip install -q pytest==9.1.1 && python -m pytest -q -p no:cacheprovider tests"
