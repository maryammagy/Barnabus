FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY src/ ./src/

# Set environment variables (no embedded secrets)
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO
ENV PORT=5001
ENV ROW_LIMIT=5000
ENV STATEMENT_TIMEOUT_MS=15000
ENV MAX_SCAN_ROWS=50000000
ENV PROVIDER_TYPE=test

EXPOSE 5001

CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--threads", "4", "src.app:app"]
