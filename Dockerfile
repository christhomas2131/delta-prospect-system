# ---- Stage 1: build the React frontend -------------------------------------
FROM node:20-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python runtime (no Node in the final image) ------------------
FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Backend modules, migrations and seed data. .dockerignore keeps secrets,
# backups, tests and local tooling out of the build context.
COPY *.py ./
COPY schema.sql ./
COPY migrations/ migrations/
COPY snapshots/ snapshots/
COPY scripts/ scripts/
COPY --from=frontend-build /build/dist frontend/dist

ENV PYTHONUNBUFFERED=1 \
    APP_ENV=production

EXPOSE ${PORT:-8000}

# Readiness: GET /api/health (DB + migrations). Liveness: GET /api/health/live.
# The same image runs the worker standalone with: python worker.py
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
