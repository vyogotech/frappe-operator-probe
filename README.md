# frappe-operator-probe

A tiny Frappe app plus a runner that exercises **every frappe-operator custom
resource** against a live cluster and asserts, through the app's own API, that
each one actually did its job.

| CR | What the probe checks |
|---|---|
| FrappeBench | reaches Ready with git installs enabled and `commonSiteConfig` (server scripts on) |
| FrappeSite | reaches Ready and answers `/api/method/ping` on its public host |
| SiteRole / SiteUser / SiteAPIKey | role exists, user carries it, API key Secret authenticates as Administrator |
| SiteApp | `vyogo_probe` installed from git; `autoMigrate` ran migrate (an `after_migrate` record appears) |
| SiteConfig | `customConfig` marker, `secretConfig` value (compared by sha256), `maxFileSize` all in `site_config.json` |
| SiteCustomField / SitePropertySetter | field `probe_extra` on Probe Record; `title` made required |
| SiteServerScript | a Before Insert script stamps every inserted record |
| SiteClientScript | script present and enabled |
| SiteWebhook | after_insert webhook posts back into the site; delivery observed |
| SiteQuota | status reports usage |
| SiteUserPermission | permission scoped to a seeded record |
| SiteCron | `*/2` job runs `vyogo_probe.tasks.tick`; a `cron` record appears |
| SiteMigration | `after_migrate` hook leaves a `migrate` record |
| SiteBackup + SiteRestore | seed 25 records, back up, wipe, restore, checksum identical |
| SiteDomain | alias host served by the same site |

## Run it

```bash
./probe.py --domain vyogo.cloud --mariadb-ref frappe-mariadb/mariadb --kubeconfig ~/hub.yaml --keep-going
./probe.py ... --cleanup            # tear the namespace down afterwards (site DB is deleted too)
./probe.py ... --only config,cron   # re-run phases against an existing site
```

Prerequisites on the cluster: frappe-operator ≥ v5.2.1 (SiteConfig `secretConfig`),
a MariaDB CR to point `--mariadb-ref` at, an ingress class, and public DNS for
`<site>.<domain>` and `<site>-alias.<domain>` (the webhook and domain phases
call the site from outside). Needs `kubectl` and `curl` locally.

## The app

`vyogo_probe` has one DocType, **Probe Record** (`kind` = seed | cron | patch |
migrate | heartbeat | webhook | manual), and these endpoints:

- `vyogo_probe.api.status` — everything observable, one JSON document
- `vyogo_probe.api.seed` / `checksum` / `wipe` — deterministic data for backup/restore
- `vyogo_probe.api.echo_host` (guest) — which Host and site served the request
- `vyogo_probe.api.webhook_sink` (guest, POST) — target for the site's own webhook

Hooks: `after_migrate` and a one-time patch (SiteMigration), an hourly
scheduler event, `doc_events` on Probe Record.

## What it has found so far

Run against the vyogo.cloud hub on 2026-09-14 (frappe-operator release branch,
after v5.2.1), the probe surfaced and drove the fixes for:

- a FrappeSite named like its FrappeBench reused the bench's init Job and was Ready without ever being created
- six content controllers required a `<site>-admin-password` Secret instead of the site's `adminPasswordSecretRef`
- SiteAPIKey wrote placeholder credentials and never called Frappe
- Client Script and Webhook creates lacked the document name; the webhook event went into a field Frappe ignores
- SiteApp `autoMigrate` was accepted and ignored; `backupBeforeInstall: false` could not be expressed
- site Jobs rewrote `apps.txt` from the image, so `bench migrate` deleted a SiteApp-installed app's DocTypes as orphans
- finalizers looped forever once the namespace was terminating
- SiteUserPermission failed with a duplicate on every re-reconcile (hash-named document)
- SiteRestore ignored a missing `benchRef.namespace` and could not read a cross-namespace MariaDB root Secret
- no way to enable Server Scripts on an operator-made bench (now `FrappeBench.spec.commonSiteConfig`)

Keep running it after every operator change; a green table is the contract.

