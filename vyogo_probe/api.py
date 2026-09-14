"""Whitelisted endpoints the probe runner reads. Everything a CR is expected to
have changed on the site is reported here, so a test never has to shell into
a pod to know whether a CustomField, ServerScript, Cron, Backup… did its job."""

import hashlib
import json

import frappe
from frappe.utils import now_datetime

from vyogo_probe import __version__
from vyogo_probe.vyogo_probe.doctype.probe_record.probe_record import add

DT = "Probe Record"


def _require_manager():
    if "System Manager" not in frappe.get_roles():
        frappe.throw("System Manager required", frappe.PermissionError)


@frappe.whitelist()
def status():
    """One dict with every observable the CR tests assert on."""
    _require_manager()
    conf = frappe.conf
    probe_keys = sorted(k for k in conf.keys() if str(k).startswith("probe_"))
    secret = conf.get("probe_secret")
    by_kind = {r.kind: r.n for r in frappe.db.sql(f"select kind, count(*) n from `tab{DT}` group by kind", as_dict=True)}
    last = {r.kind: str(r.t) for r in frappe.db.sql(f"select kind, max(creation) t from `tab{DT}` group by kind", as_dict=True)}
    meta = frappe.get_meta(DT)
    return {
        "app": {"name": "vyogo_probe", "version": __version__},
        "site": frappe.local.site,
        "now": str(now_datetime()),
        "installed_apps": frappe.get_installed_apps(),
        "config": {
            "probe_keys": probe_keys,
            "probe_marker": conf.get("probe_marker"),
            "probe_secret_sha256": hashlib.sha256(str(secret).encode()).hexdigest() if secret else None,
            "server_script_enabled": bool(conf.get("server_script_enabled")),
            "maintenance_mode": conf.get("maintenance_mode"),
            "max_file_size": conf.get("max_file_size"),
        },
        "records": {"total": sum(by_kind.values()), "by_kind": by_kind, "last": last, "seed_checksum": checksum()},
        "script_marks": frappe.db.count(DT, {"note": "server-script"}),
        "custom_fields": [f.fieldname for f in meta.get_custom_fields()],
        "property_setters": [
            {"field": p.field_name, "property": p.property, "value": p.value}
            for p in frappe.get_all("Property Setter", filters={"doc_type": DT}, fields=["field_name", "property", "value"])
        ],
        "roles": {r: bool(frappe.db.exists("Role", r)) for r in ("Probe Operator",)},
        "users": {
            u.name: sorted(frappe.get_roles(u.name))
            for u in frappe.get_all("User", filters={"name": ["like", "%probe%"]}, fields=["name"])
        },
        "user_permissions": frappe.get_all(
            "User Permission", filters={"allow": DT}, fields=["user", "allow", "for_value", "apply_to_all_doctypes"]
        ),
        "webhooks": frappe.get_all(
            "Webhook", filters={"webhook_doctype": DT}, fields=["webhook_docevent", "request_url", "enabled"]
        ),
        "server_scripts": frappe.get_all(
            "Server Script", filters={"reference_doctype": DT}, fields=["name", "script_type", "doctype_event", "disabled"]
        ),
        "client_scripts": frappe.get_all("Client Script", filters={"dt": DT}, fields=["name", "enabled"]),
        "api_keys": {
            u.name: bool(u.api_key)
            for u in frappe.get_all("User", filters={"api_key": ["is", "set"]}, fields=["name", "api_key"])
        },
        "scheduler_disabled": bool(frappe.utils.scheduler.is_scheduler_disabled()),
    }


@frappe.whitelist()
def seed(count: int = 25):
    """Deterministic records for the backup/restore round trip."""
    _require_manager()
    count = int(count)
    names = []
    for i in range(1, count + 1):
        names.append(add("seed", payload=f"seed-payload-{i:04d}", seq=i, title=f"seed {i:04d}"))
    frappe.db.commit()
    return {"created": len(names), "first": names[0] if names else None, "checksum": checksum()}


@frappe.whitelist()
def checksum():
    """sha256 over the seed records' (seq, payload); identical before backup and after restore."""
    rows = frappe.get_all(DT, filters={"kind": "seed"}, fields=["seq", "payload"], order_by="seq asc")
    blob = json.dumps([[r.seq, r.payload] for r in rows], separators=(",", ":")).encode()
    return {"count": len(rows), "sha256": hashlib.sha256(blob).hexdigest()}


@frappe.whitelist()
def wipe(kind: str = "seed"):
    _require_manager()
    n = frappe.db.count(DT, {"kind": kind})
    frappe.db.delete(DT, {"kind": kind})
    frappe.db.commit()
    return {"deleted": n}


@frappe.whitelist(allow_guest=True)
def echo_host():
    """SiteDomain: what Host did this request arrive on, and which site served it."""
    return {"host": frappe.request.host, "site": frappe.local.site}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook_sink():
    """SiteWebhook target: whatever the site's own webhook posts lands here as a record."""
    body = frappe.request.get_data(as_text=True) or ""
    add("webhook", payload=body[:2000], note="webhook")
    frappe.db.commit()
    return {"ok": True}
