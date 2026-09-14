import frappe


def execute():
    """Runs once per site: the marker a SiteMigration test looks for."""
    if not frappe.db.exists("DocType", "Probe Record"):
        return
    from vyogo_probe.vyogo_probe.doctype.probe_record.probe_record import add

    add("patch", payload="vyogo_probe.patches.mark_migrated")
