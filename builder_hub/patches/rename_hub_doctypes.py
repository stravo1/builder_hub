"""Drop the redundant `Builder Hub` prefix from the module's DocTypes.

Runs before model sync so the renamed tables carry their existing rows
instead of the sync creating empty `Hub *` tables beside the old ones.
Document names (publisher IDs, extension names) are unchanged, so Link
values stay valid; `sync_customizations` refreshes the field options.
"""

import frappe

RENAMES = {
	"Builder Hub Publisher": "Hub Publisher",
	"Builder Hub Extension": "Hub Extension",
	"Builder Hub Extension Release": "Hub Extension Release",
	"Builder Hub Publication Request": "Hub Publication Request",
}


def execute():
	for old, new in RENAMES.items():
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, force=True)
