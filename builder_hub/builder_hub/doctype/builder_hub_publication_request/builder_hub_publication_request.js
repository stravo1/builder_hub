frappe.ui.form.on("Builder Hub Publication Request", {
	refresh(form) {
		if (form.is_new() || form.doc.status !== "Pending Review") {
			return;
		}

		form.add_custom_button(
			__("Approve"),
			() => review_request(form, "approve_publication_request"),
			__("Actions"),
		);
		form.add_custom_button(
			__("Reject"),
			() => {
				frappe.prompt(
					{
						fieldname: "reason",
						fieldtype: "Small Text",
						label: __("Reason"),
						reqd: 1,
					},
					({ reason }) => review_request(form, "reject_publication_request", reason),
					__("Reject publication request"),
				);
			},
			__("Actions"),
		);
	},
});

function review_request(form, action, reason = null) {
	return frappe
		.call({
			method: `builder_hub.extensions.api.${action}`,
			args: { request_name: form.doc.name, reason },
			freeze: true,
		})
		.then(() => form.reload_doc());
}
