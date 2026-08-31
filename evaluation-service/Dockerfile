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
ENV PORT=5000
ENV ALERT_BUDGET=10
ENV ALERT_WINDOW_HOURS=24
ENV DRIFT_THRESHOLD=0.1
ENV CALIBRATION_THRESHOLD=0.05
ENV OUTCOME_THRESHOLD=0.1
ENV BASELINE_WINDOW_DAYS=30
ENV P99_LATENCY_BUDGET_MS=500
ENV STATEMENT_TIMEOUT_MS=15000

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "src.app:app"]
