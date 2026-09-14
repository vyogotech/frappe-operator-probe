#!/usr/bin/env python3
"""frappe-operator probe: drive every CR against a live cluster and assert its
effect through the vyogo_probe app's API. Needs kubectl (+ a kubeconfig) and curl.

  ./probe.py --namespace probe --domain vyogo.cloud --mariadb-ref frappe-mariadb/mariadb
  ./probe.py ... --cleanup          # delete the namespace at the end (site DB too)
  ./probe.py ... --only site,siteapp,config

Each phase prints PASS/FAIL with what it observed; exit code is non-zero if any fail.
"""
import argparse, functools, hashlib, json, os, secrets, subprocess, sys, time, urllib.parse

print = functools.partial(print, flush=True)  # progress must show while phases run

HERE = os.path.dirname(os.path.abspath(__file__))
DONE = {"Ready", "Succeeded", "Completed", "Active", "Normal", "Scheduled"}
BAD = {"Failed", "Error"}


def sh(*args, input=None, check=True):
    r = subprocess.run(args, input=input, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}… failed: {r.stderr.strip()[:400]}")
    return r.stdout


class Probe:
    def __init__(self, a):
        self.a = a
        self.run_id = a.run_id or time.strftime("%m%d%H%M")
        self.vars = {
            "NAMESPACE": a.namespace, "BENCH": a.bench, "SITE": a.site,
            "SITE_HOST": f"{a.site}.{a.domain}", "ALIAS_HOST": f"{a.site}-alias.{a.domain}",
            "SITE_URL": a.site_url or f"https://{a.site}.{a.domain}",
            "FRAPPE_VERSION": a.frappe_version, "BENCH_IMAGE_REPO": a.bench_image.rsplit(":", 1)[0],
            "BENCH_IMAGE_TAG": a.bench_image.rsplit(":", 1)[1], "STORAGE_SIZE": a.storage_size,
            "MARIADB_NAME": a.mariadb_ref.split("/")[0], "MARIADB_NAMESPACE": a.mariadb_ref.split("/")[1],
            "INGRESS_CLASS": a.ingress_class, "PROBE_REPO": a.probe_repo, "PROBE_BRANCH": a.probe_branch,
            "ADMIN_PASSWORD": a.admin_password or secrets.token_urlsafe(16),
            "PROBE_SECRET": secrets.token_hex(16), "PROBE_MARKER": f"probe-{self.run_id}", "RUN_ID": self.run_id,
        }
        self.kc = ["kubectl"] + (["--kubeconfig", a.kubeconfig] if a.kubeconfig else [])
        self.results = []
        self.token = None

    # ---- k8s helpers ----
    def render(self, name, extra=None):
        t = open(os.path.join(HERE, "manifests", name)).read()
        for k, v in {**self.vars, **(extra or {})}.items():
            t = t.replace("${" + k + "}", str(v))
        return t

    def apply(self, name, extra=None):
        sh(*self.kc, "apply", "-f", "-", input=self.render(name, extra))

    def get(self, kind, name):
        out = sh(*self.kc, "-n", self.a.namespace, "get", kind, name, "-o", "json", check=False)
        return json.loads(out) if out.strip().startswith("{") else {}

    def wait(self, kind, name, done=DONE, timeout=900, poll=10):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.get(kind, name).get("status", {})
            ph = st.get("phase", "")
            if ph in done:
                return st
            if ph in BAD:
                raise RuntimeError(f"{kind}/{name} phase {ph}: {st.get('message') or [c.get('message') for c in st.get('conditions', [])][-1:]}")
            time.sleep(poll)
        raise RuntimeError(f"{kind}/{name} not {sorted(done)} after {timeout}s (phase={ph!r})")

    # ---- site API helpers ----
    def curl(self, path, method="GET", data=None, host=None, auth=True, timeout=60):
        base = self.vars["SITE_URL"] if not host else f"{urllib.parse.urlparse(self.vars['SITE_URL']).scheme}://{host}"
        cmd = ["curl", "-s", "-m", str(timeout), "-X", method, base + path, "-H", "Accept: application/json"]
        if self.a.edge_ip:
            h = urllib.parse.urlparse(base).hostname
            cmd += ["--resolve", f"{h}:443:{self.a.edge_ip}", "--resolve", f"{h}:80:{self.a.edge_ip}"]
        if auth and self.token:
            cmd += ["-H", f"Authorization: token {self.token}"]
        if data is not None:
            cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
        out = sh(*cmd, check=False)
        try:
            return json.loads(out)
        except Exception:
            return {"_raw": out[:300]}

    def call(self, method, **kw):
        r = self.curl(f"/api/method/{method}", method="POST", data=kw)
        if "message" not in r:
            raise RuntimeError(f"{method}: {r.get('exception') or r.get('_raw') or r}")
        return r["message"]

    def status(self):
        return self.call("vyogo_probe.api.status")

    def until(self, pred, timeout=300, poll=10, what="condition"):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                v = pred()
                if v:
                    return v
            except Exception as e:  # noqa
                last = e
            time.sleep(poll)
        raise RuntimeError(f"timed out waiting for {what}")

    # ---- phases ----
    def phase(self, name, fn):
        if self.a.only and name not in self.a.only:
            return
        t0 = time.time()
        try:
            detail = fn() or ""
            self.results.append((name, "PASS", f"{time.time()-t0:5.0f}s  {detail}"))
            print(f"PASS {name:12s} {detail}")
        except Exception as e:
            self.results.append((name, "FAIL", f"{time.time()-t0:5.0f}s  {e}"))
            print(f"FAIL {name:12s} {e}")
            if not self.a.keep_going:
                raise

    def p_bench(self):
        self.apply("00-namespace.yaml"); self.apply("10-bench.yaml")
        st = self.wait("frappebench", self.a.bench, timeout=1200)
        return f"bench Ready, gitEnabled={st.get('gitEnabled')}"

    def p_site(self):
        self.apply("20-site.yaml")
        self.wait("frappesite", self.a.site, timeout=1200)
        r = self.until(lambda: self.curl("/api/method/ping", auth=False).get("message") == "pong", 300, what="public ping")
        return f"site Ready, {self.vars['SITE_URL']} answers"

    def p_access(self):
        self.apply("50-role-user-apikey.yaml")
        for kind, n in (("siterole", f"{self.a.site}-probe-operator"), ("siteuser", f"{self.a.site}-probe-user"), ("siteapikey", f"{self.a.site}-admin-key")):
            self.wait(kind, n, timeout=300)
        sec = self.until(lambda: self.get("secret", f"{self.a.site}-admin-api-key").get("data"), 120, what="api key secret")
        import base64
        self.token = base64.b64decode(sec["api_key"]).decode() + ":" + base64.b64decode(sec["api_secret"]).decode()
        me = self.curl("/api/method/frappe.auth.get_logged_user")
        assert me.get("message") == "Administrator", me
        return "SiteRole + SiteUser Ready, SiteAPIKey works (Administrator)"

    def p_siteapp(self):
        self.apply("30-siteapp.yaml")
        self.wait("siteapp", f"{self.a.site}-vyogo-probe", timeout=1200)
        s = self.until(lambda: (lambda st: st if "vyogo_probe" in st["installed_apps"] else None)(self.status()), 120, what="app in installed_apps")
        k = s["records"]["by_kind"]
        assert k.get("patch", 0) >= 1, f"patch marker missing: {k}"
        return f"vyogo_probe {s['app']['version']} installed; patch ran (records={k})"

    def p_config(self):
        self.apply("40-siteconfig.yaml")
        self.wait("siteconfig", self.a.site, timeout=600)
        s = self.status()["config"]
        want = hashlib.sha256(self.vars["PROBE_SECRET"].encode()).hexdigest()
        assert s["probe_marker"] == self.vars["PROBE_MARKER"], s
        assert s["probe_secret_sha256"] == want, "secretConfig value differs"
        assert s["server_script_enabled"] and int(s["max_file_size"] or 0) == 10485760, s
        return f"customConfig + secretConfig + maxFileSize applied ({s['probe_keys']})"

    def p_content(self):
        self.apply("55-schema-and-scripts.yaml")
        for kind, n in (("sitecustomfield", "probe-extra"), ("sitepropertysetter", "title-reqd"), ("siteserverscript", "mark-inserts"),
                        ("siteclientscript", "list-banner"), ("sitewebhook", "on-insert")):
            self.wait(kind, f"{self.a.site}-{n}", timeout=300)
        self.wait("sitequota", f"{self.a.site}-quota", done={"Normal", "QuotaExceeded", "Ready"}, timeout=300)
        s = self.status()
        assert "probe_extra" in s["custom_fields"], s["custom_fields"]
        assert any(p["field"] == "title" and p["property"] == "reqd" and str(p["value"]) == "1" for p in s["property_setters"]), s["property_setters"]
        assert s["server_scripts"] and not s["server_scripts"][0]["disabled"], s["server_scripts"]
        assert s["client_scripts"], "client script missing"
        assert any(w["webhook_docevent"] == "after_insert" and w["enabled"] for w in s["webhooks"]), s["webhooks"]
        assert s["roles"]["Probe Operator"], "role missing"
        u = f"probe-user@{self.vars['SITE_HOST']}"
        assert "Probe Operator" in s["users"].get(u, []), s["users"]
        q = self.get("sitequota", f"{self.a.site}-quota")["status"]
        return f"custom field, property setter, server+client script, webhook, role/user, quota(users={q.get('currentUsers')})"

    def p_seed(self):
        r = self.call("vyogo_probe.api.seed", count=25)
        self.seed_checksum = r["checksum"]
        s = self.until(lambda: (lambda st: st if st["script_marks"] >= 25 else None)(self.status()), 60, what="server script marks")
        w = self.until(lambda: (lambda st: st if st["records"]["by_kind"].get("webhook", 0) >= 1 else None)(self.status()), 240, what="webhook delivery (background worker)")
        self.apply("56-user-permission.yaml", {"SEED_RECORD": r["first"]})
        self.wait("siteuserpermission", f"{self.a.site}-probe-user-scope", timeout=300)
        s = self.status()
        assert any(p["for_value"] == r["first"] for p in s["user_permissions"]), s["user_permissions"]
        return f"25 seeds, server script marked {s['script_marks']}, webhook delivered {s['records']['by_kind'].get('webhook')}, user permission on {r['first']}"

    def p_cron(self):
        self.apply("60-cron.yaml")
        self.wait("sitecron", f"{self.a.site}-tick", done={"Scheduled", "Ready", "Active"}, timeout=300)
        s = self.until(lambda: (lambda st: st if st["records"]["by_kind"].get("cron", 0) >= 1 else None)(self.status()), 420, what="first cron tick (schedule */2)")
        return f"SiteCron scheduled, ticks={s['records']['by_kind']['cron']}"

    def p_migration(self):
        before = self.status()["records"]["by_kind"].get("migrate", 0)
        self.apply("70-migration.yaml")
        self.wait("sitemigration", f"{self.a.site}-migrate-{self.run_id}", timeout=900)
        after = self.status()["records"]["by_kind"].get("migrate", 0)
        assert after > before, f"after_migrate did not run ({before}->{after})"
        return f"SiteMigration Succeeded, after_migrate records {before}->{after}"

    def p_backup_restore(self):
        chk = self.call("vyogo_probe.api.checksum")
        assert chk["count"] == 25, chk
        path = f"sites/{self.vars['SITE_HOST']}/private/backups/{self.run_id}/database.sql.gz"
        self.apply("80-backup.yaml", {"BACKUP_DB_PATH": path})
        self.wait("sitebackup", f"{self.a.site}-backup-{self.run_id}", timeout=900)
        self.call("vyogo_probe.api.wipe", kind="seed")
        assert self.call("vyogo_probe.api.checksum")["count"] == 0
        self.apply("90-restore.yaml", {"BACKUP_DB_PATH": path})
        self.wait("siterestore", f"{self.a.site}-restore-{self.run_id}", timeout=900)
        got = self.until(lambda: (lambda c: c if c["count"] == 25 else None)(self.call("vyogo_probe.api.checksum")), 180, what="restored rows")
        assert got["sha256"] == chk["sha256"], "checksum differs after restore"
        return f"backup -> wipe -> restore, checksum {chk['sha256'][:12]} intact"

    def p_domain(self):
        self.apply("95-domain.yaml")
        self.wait("sitedomain", f"{self.a.site}-alias", timeout=600)
        r = self.until(lambda: (lambda x: x if x.get("message", {}).get("host", "").startswith(self.vars["ALIAS_HOST"]) else None)(
            self.curl("/api/method/vyogo_probe.api.echo_host", host=self.vars["ALIAS_HOST"], auth=False)), 300, what="alias host routing")
        return f"SiteDomain serves {r['message']['host']} for site {r['message']['site']}"

    def cleanup(self):
        sh(*self.kc, "delete", "namespace", self.a.namespace, "--wait=false", check=False)

    def run(self):
        print(f"run {self.run_id}: ns={self.a.namespace} site={self.vars['SITE_HOST']} url={self.vars['SITE_URL']}")
        for name, fn in (("bench", self.p_bench), ("site", self.p_site), ("access", self.p_access), ("siteapp", self.p_siteapp),
                         ("config", self.p_config), ("content", self.p_content), ("seed", self.p_seed), ("cron", self.p_cron),
                         ("migration", self.p_migration), ("backup", self.p_backup_restore), ("domain", self.p_domain)):
            self.phase(name, fn)
        print("\n== summary ==")
        for n, r, d in self.results:
            print(f"{r:4s} {n:12s} {d}")
        if self.a.cleanup:
            self.cleanup(); print("namespace deletion requested")
        return 0 if all(r == "PASS" for _, r, _ in self.results) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", default="probe"); ap.add_argument("--bench", default="probe-bench"); ap.add_argument("--site", default="probe")
    ap.add_argument("--domain", required=True, help="wildcard zone the site is published on, e.g. vyogo.cloud")
    ap.add_argument("--site-url", help="override https://<site>.<domain>")
    ap.add_argument("--edge-ip", help="curl --resolve every hostname to this IP (bypasses local DNS caches)")
    ap.add_argument("--mariadb-ref", required=True, help="name/namespace of the MariaDB CR to use")
    ap.add_argument("--frappe-version", default="16"); ap.add_argument("--bench-image", default="ghcr.io/vyogotech/frappe-for-operator:version-16")
    ap.add_argument("--storage-size", default="5Gi"); ap.add_argument("--ingress-class", default="nginx")
    ap.add_argument("--probe-repo", default="https://github.com/vyogotech/frappe-operator-probe"); ap.add_argument("--probe-branch", default="main")
    ap.add_argument("--admin-password"); ap.add_argument("--run-id"); ap.add_argument("--kubeconfig")
    ap.add_argument("--only", type=lambda s: set(s.split(",")), help="comma list of phases")
    ap.add_argument("--keep-going", action="store_true", help="continue after a failed phase")
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    sys.exit(Probe(a).run())


if __name__ == "__main__":
    main()
