FROM python:3.12-slim

ARG RD_RELEASE=1.2.0
ARG RD_REVISION=local
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV RD_RELEASE=${RD_RELEASE} RD_REVISION=${RD_REVISION}
LABEL org.opencontainers.image.title="RDIC CRM" \
      org.opencontainers.image.version="${RD_RELEASE}" \
      org.opencontainers.image.revision="${RD_REVISION}"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-production.txt
COPY . .
# /backups must exist and be owned by rdic so a fresh named volume inherits it.
RUN useradd --create-home --uid 10001 rdic && mkdir -p /backups \
    && chown -R rdic:rdic /app /backups
USER rdic
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:application"]
