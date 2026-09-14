import frappe
from frappe.model.document import Document


class ProbeRecord(Document):
    pass


def add(kind: str, payload: str = "", seq: int = 0, title: str = "", note: str = "") -> str:
    """Insert a record without permission checks; returns its name."""
    doc = frappe.get_doc(
        {"doctype": "Probe Record", "kind": kind, "payload": payload, "seq": seq, "title": title, "note": note}
    )
    doc.insert(ignore_permissions=True)
    return doc.name
