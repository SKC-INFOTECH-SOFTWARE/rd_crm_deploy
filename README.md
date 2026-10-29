# RDIC CRM

Internal sales CRM for R D Industrial Corporation (RDIC, former proprietorship, and
RD, its successor partnership). It reads accounting data from the **Balance API**
(read-only, nothing is ever written back) and adds clients, follow-ups, opportunities,
versioned quotations with Excel export, purchase/payment history and role-based access.

## Architecture

| Service | What it does |
|---|---|
| `database` | PostgreSQL 17 (data in the `postgres_data` volume) |
| `migrate` | One-shot schema migration before the app starts |
| `web` | Flask app on gunicorn (internal port 8000) |
| `scheduler` | Hourly incremental Balance sync and the manual sync queue |
| `backup` | Daily verified `pg_dump` into the `backups` volume, 30-day retention |
| `caddy` | Front proxy with security headers. **Only host port 7060 is published** |

Code layout: `rd_web` (UI, auth), `rd_crm` (CRM domain), `rd_import` (Balance client,
importer, scheduler), `rd_stock` (recipes, stock schema), `rd_dashboard` (read helpers),
`rd_ops` (migrate, preflight, backup, integrity). `test_api.py` is the Balance HTTP
client and diagnostic CLI, not a test file.

## First deployment (Linux server with Docker)

```bash
git clone <repo-url> rdic-crm && cd rdic-crm
sh deploy/first-deploy.sh
```

The script creates `.env` with generated secrets, creates placeholder runtime-config
files, builds the image, migrates, seeds the first administrator, runs the production
preflight and starts everything. Open `http://<server-ip>:7060`.

Initial login: **admin / admin**. This is a temporary seed password; change it from
**Users** right after the first login, then clear `RD_ADMIN_PASSWORD` in `.env`.
Re-running the script is safe: existing `.env`, config, data and users are kept.

Before go-live, set the real values (ask the client):

- `runtime-config/import_config.json` → `base_url` of the production Balance API
- `.env` → `RD_SUBSCRIPTION_KEY`, and `secrets/private_key.pem` if `decryption_mode` is `local`
- `runtime-config/*.json` → reviewed client overrides, identity groups, ignored clients,
  profile enrichment and supplier directory (empty placeholders are valid)

Then: `docker compose restart scheduler web`.

## Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `RD_PORT` | Host port, default `7060` |
| `RD_COOKIE_SECURE` | `false` for direct `http://ip:7060` access; `true` once HTTPS is in front |
| `POSTGRES_PASSWORD`, `RD_DATABASE_URL` | Database credentials (generated) |
| `RD_SECRET_KEY` | Session signing key, 32+ characters (generated) |
| `RD_RELEASE`, `RD_IMAGE`, `RD_REVISION` | Must match `VERSION` and the deployed commit |
| `RD_ADMIN_*` | First administrator, used only when no user exists |

## HTTPS with a domain (optional)

Keep the app on 7060 and let the server's existing nginx terminate TLS:

```nginx
server {
    server_name crm.example.com;
    location / {
        proxy_pass http://127.0.0.1:7060;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    # listen 443 ssl; certificates via certbot --nginx
}
```

Then set `RD_COOKIE_SECURE=true` and run `docker compose up -d web`.

## Updates

```bash
git pull
# set RD_REVISION in .env to: git rev-parse --short=12 HEAD
# if VERSION changed, also update RD_RELEASE and RD_IMAGE
sh deploy/update.sh
```

`update.sh` takes a verified backup before migrating, runs the preflight and waits for
readiness.

## Operations

```bash
docker compose ps                                  # status
docker compose logs -f web scheduler               # logs (JSON)
curl http://127.0.0.1:7060/health/ready            # readiness
docker compose run --rm migrate python -m rd_ops preflight --production
docker compose run --rm backup python -m rd_ops backup        # manual backup
docker compose run --rm migrate python -m rd_web.admin reset-password --username admin
```

Roles: Admin/Owner, Management, Sales, External Sales, Accounts, Operations, Read-only.
There is no public sign-up and no self-service password reset; administrators create
users and reset passwords from **Users**.

## Local development (Windows, without Docker)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-production.txt
.\.venv\Scripts\python.exe -m rd_ops migrate
$env:RD_SECRET_KEY = '<long-random-secret>'
$env:RD_ADMIN_USERNAME = 'admin'; $env:RD_ADMIN_DISPLAY_NAME = 'Administrator'; $env:RD_ADMIN_PASSWORD = 'admin'
.\.venv\Scripts\python.exe -m rd_web.admin bootstrap-admin --if-missing --allow-weak-password
.\run_dashboard.ps1        # http://127.0.0.1:8501 (SQLite in data/)
```
