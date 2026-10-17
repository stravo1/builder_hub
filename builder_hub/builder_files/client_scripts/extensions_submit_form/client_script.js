(() => {
	const form = document.querySelector("[data-publication-form]");
	const button = form.querySelector("button[type=submit]");
	const result = form.querySelector("[data-publication-result]");
	const label = button.innerHTML;
	const spinner = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>`;
	const icons = {
		success: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#43AC79" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>`,
		error: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#E03434" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/></svg>`,
	};

	function setBusy(isBusy) {
		button.disabled = isBusy;
		button.style.pointerEvents = isBusy ? "none" : "";
		button.innerHTML = isBusy ? `${spinner}<span>Checking repository…</span>` : label;
		if (isBusy) {
			button.querySelector("svg").animate(
				[{ transform: "rotate(0deg)" }, { transform: "rotate(360deg)" }],
				{ duration: 800, iterations: Infinity },
			);
		}
	}

	function showResult(message, isError) {
		const text = document.createElement("span");
		text.textContent = message;
		result.innerHTML = icons[isError ? "error" : "success"];
		result.append(text);
		result.style.display = "grid";
		result.style.backgroundColor = isError ? "#FFE7E7" : "#E4FAEB";
	}

	function getErrorMessage(body) {
		try {
			return JSON.parse(JSON.parse(body._server_messages)[0]).message;
		} catch (error) {
			return "The publication request could not be submitted.";
		}
	}

	form.addEventListener("submit", async (event) => {
		event.preventDefault();
		const data = new FormData(form);
		const categories = String(data.get("categories") || "")
			.split(",")
			.map((category) => category.trim())
			.filter(Boolean);
		setBusy(true);
		result.style.display = "none";
		try {
			const response = await fetch("/api/method/builder_hub.extensions.api.request_publication", {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": frappe.csrf_token || "",
				},
				body: JSON.stringify({
					repository_url: data.get("repository_url"),
					categories: JSON.stringify(categories),
				}),
			});
			const body = await response.json();
			if (!response.ok) throw new Error(getErrorMessage(body));
			showResult(`Request received for ${body.message.extension_name}. A maintainer will review it.`, false);
			form.reset();
		} catch (error) {
			showResult(error.message, true);
		} finally {
			setBusy(false);
		}
	});
})();
