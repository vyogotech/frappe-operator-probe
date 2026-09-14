"""Things the platform triggers on its own; each leaves a Probe Record behind."""

import frappe
from frappe.utils import now_datetime


def _add(kind, payload="", note=""):
    from vyogo_probe.vyogo_probe.doctype.probe_record.probe_record import add

    return add(kind, payload=payload, note=note)


def tick():
    """SiteCron target: `bench --site <site> execute vyogo_probe.tasks.tick`."""
    name = _add("cron", payload=str(now_datetime()))
    frappe.db.commit()
    return name


def heartbeat():
    """scheduler_events hourly."""
    _add("heartbeat", payload=str(now_datetime()))


def record_migrate():
    """after_migrate hook: proves a SiteMigration actually ran migrate."""
    if frappe.db.exists("DocType", "Probe Record"):
        _add("migrate", payload=str(now_datetime()))
        frappe.db.commit()


def on_probe_insert(doc, method=None):
    # Nothing to do; the hook exists so `doc_events` is exercised on every insert.
    return None
