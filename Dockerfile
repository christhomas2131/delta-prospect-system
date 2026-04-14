FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Install Node.js for frontend build
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium --with-deps

# Build frontend
COPY frontend/ frontend/
RUN cd frontend && npm ci && npm run build

# Copy backend files
COPY api.py .
COPY asx_scraper.py .
COPY asx_browser.py .
COPY enrichment_agent.py .
COPY deep_analysis.py .
COPY schema.sql .

EXPOSE ${PORT:-8000}

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
