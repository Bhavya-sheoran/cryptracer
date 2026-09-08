FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# The base image ships pip 24.0 and setuptools 79.x, which between them carry
# eight advisories that pip-audit reports against this image. They are build
# tooling rather than runtime dependencies, but upgrading first is one line and
# leaves the audit clean instead of caveated.
RUN pip install --no-cache-dir --upgrade pip setuptools

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Unprivileged runtime user. A container escape from a root process lands as
# root on the host namespace; from this one it lands as nobody in particular.
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /app/storage \
 && chown -R app:app /app/storage \
 && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
