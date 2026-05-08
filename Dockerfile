FROM python:3.11-slim

# Install curl + Node.js for frontend build
RUN apt-get update && \
    apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build frontend
COPY frontend/ frontend/
RUN cd frontend && npm ci && npm run build

# Copy backend files
COPY api.py .
COPY asx_scraper.py .
COPY asx_browser.py .
COPY enrichment_agent.py .
COPY deep_analysis.py .
COPY prize_calculator.py .
COPY v3_intelligence.py .
COPY schema.sql .
COPY snapshots/ snapshots/

EXPOSE ${PORT:-8000}

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
