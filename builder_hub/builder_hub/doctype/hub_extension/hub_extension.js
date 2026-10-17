frappe.ui.form.on("Hub Extension", {
	refresh(form) {
		if (form.is_new() || !["Published", "Deprecated"].includes(form.doc.status)) {
			return;
		}

		form.add_custom_button(
			__("Check for New Releases"),
			() => {
				frappe
					.call({
						method: "builder_hub.extensions.api.request_release_check",
						args: { extension_name: form.doc.name },
						freeze: true,
					})
					.then(() =>
						frappe.show_alert({
							message: __("The release check is queued. Reload the form to see new releases."),
							indicator: "green",
						}),
					);
			},
			__("Actions"),
		);
	},
});
