# Runtime configuration

Production mounts this directory read-only into the application containers. Its JSON
files are ignored by Git because they hold company-specific operational decisions.
`deploy/first-deploy.sh` creates any missing file as a valid empty placeholder.

| File | Purpose |
|---|---|
| `import_config.json` | Balance API `base_url`, company codes, decryption mode (start from `config.example.json`) |
| `client_data_overrides.json` | Confirmed per-client profile/voucher corrections (`version 1`, `clients`) |
| `client_identity_groups.json` | CRM profiles confirmed to be the same customer (`version 2`, `groups`) |
| `ignored_clients.json` | Client names hidden from the CRM (`version 1`, `names`) |
| `client_profile_enrichment.json` | Explicit, idempotent profile enrichment records (`version 1`) |
| `supplier_directory.json` | Allowlist for the Suppliers page (`version 1`, `suppliers`) |

Keep an encrypted backup of this directory. After every edit run:

```bash
docker compose run --rm --no-deps migrate python -m rd_ops preflight --production
```

The web process reloads ignored-client, identity-group, data-override and supplier files
when they change; no rebuild is needed. Restart the scheduler after editing
`import_config.json`. Identity-group merges still require the explicit
`python -m rd_crm.identity_groups --apply` workflow.
