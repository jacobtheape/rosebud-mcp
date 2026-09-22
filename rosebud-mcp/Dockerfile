FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Live browser checkout: Chromium plus the OS libraries headless
# Chromium needs. (Fake mode, the default, never touches this.)
RUN playwright install --with-deps chromium
COPY server.py fsn_client.py .
COPY checkout/ ./checkout/

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
