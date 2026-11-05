FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY allowance ./allowance
COPY web ./web
ENV ALLOWANCE_DB=/data/allowance.db PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
