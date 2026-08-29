# Multi-stage: build wheels once, ship a slim non-root runtime.
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
# curl_cffi ships its own curl-impersonate binary; no system curl needed.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=build /install /usr/local
COPY flight.py linkedin_profile.py linkedin_client.py cache.py api.py ./
# The cache file lives in appuser's home, which is writable; mount a volume
# there (or override CACHE_DB) to keep the cache across redeploys.
ENV CACHE_DB=/home/appuser/cache.db
USER appuser

ENV PORT=8080
EXPOSE 8080
# Shell form so ${PORT} is expanded by the platform at runtime.
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT}
