# Builder Hub extension operations

The site configuration key `builder_hub_github_token` should contain a fine-grained GitHub token with read-only access to public repository metadata and releases. The token is never returned by the API or sent to redirected asset hosts.

## Failed imports

1. Open the `Builder Hub Extension Release` record.
2. Read `validation_errors`. Each entry contains a stable code and an author-facing message.
3. Confirm that the repository is public, active, owned by the publisher account ID, and contains `manifest.json`, `README.md`, `LICENSE`, and `versions.json` on its default branch.
4. Confirm that the published GitHub release has the exact SemVer tag and exactly one correctly named `.builderext` asset.
5. Ask the author to publish a new version if an existing published asset must change. Published release assets and stored hashes are immutable.
6. After correcting a repository-only problem, use the listing action that calls `request_release_check` or run `builder_hub.extensions.tasks.check_repository` for the listing.

The Redis counters under `builder_hub:extensions:metrics:*` track checks, check failures, validation failures, and GitHub rate-limit failures. Logs use the `builder_hub.extensions` logger and never include the configured token.

## Emergency stops

- Yank a release when it must not be offered for new installs or updates but existing local copies may keep running.
- Block a release when Builder must stop that exact installed version after its next status check.
- Block a listing when every release is unsafe.
- Block a publisher when every owned listing is unsafe.

Each action requires a Builder Hub maintainer and creates a `Builder Hub Extension Audit` record. Never delete a published release. After a stop, verify it with `get_release_status`; catalog caches are cleared by the state-change action.
