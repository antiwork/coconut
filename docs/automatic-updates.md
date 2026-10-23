# Automatic updates after a merge

The Mac can install merged Coconut changes without a bot turn. Enable this only when the operator authorizes deployment after merge.

Run `scripts/automatic_update.py CONFIG` from an external launch agent. Set `StartInterval` to 120 seconds and `RunAtLoad` to true. Use Python 3.11 or newer. Use a separate launch agent from the Coconut service so a service restart cannot stop the updater.

The private JSON config contains:

- `repository`: the GitHub repository, such as `owner/coconut`.
- `source`: a dedicated clone for deployment. Do not use this clone for other work.
- `live`: the existing installation beneath the user's home directory.
- `gh_config_dir`: an optional private GitHub CLI configuration directory.
- `release_helper`: an optional absolute path to the operator's TestFlight release helper.
- `release_source`: a separate dedicated clone for iOS archives.

The helper must support `archive --source --receipt`, `status --build`, and `upload --source --expected-build`. The archive command must write a new receipt with its build, source, and source hash while holding its archive lock. It must validate the expected build against its archive record while holding its archive lock. Keep signing keys and App Store credentials outside the repository.

The `status --build` command uses this protocol:

- Exit code 0 means the build is available to the configured private testers. It must not mean only that Apple accepted an upload.
- A nonzero exit with JSON on stdout means Apple has the build. The JSON `processing` field reports Apple's state.
- `processing` values `FAILED` and `INVALID` mark a rejected build. Other values leave the build pending without another upload.
- A nonzero exit with `Build is not listed yet.` on stdout permits an upload attempt.
- Other failures report an error. They must not contain the not-listed text.

The upload command must check Apple for an existing build before each attempt. It must refuse a duplicate upload. An archive receipt contains `source`, `source_hash`, `build` (a positive integer or its decimal string), and `version` (one to three numeric components). The source hash uses the same input paths and SHA-256 calculation as `ios_hash()` in the updater.

The helper publishes its authoritative `archive.json` beside the helper script before exclusively creating the requested receipt. It must remove its previous archive record before starting a new archive. Before saving archive intent, the updater captures the previous record's file identity and content digest under the helper's `release.lock`. The updater holds that lock through source verification, archive invocation, and recovery; the archive child inherits the lock so an orphaned child still excludes retries. This integration calls the Python helper's `main()` with its normal archive arguments and private umask under the inherited lock, rather than reacquiring the same lock through the helper's script guard. Status and upload use the normal helper CLI.

A saved receipt path alone does not prove that archive ran. With saved attempt provenance, a missing receipt and an absent or unchanged helper record permit an archive retry without uploading. A new matching helper record can recover a missing, empty, or partial receipt. Recovery validates the exact source commit, input hash, record schema, and all surviving receipt identity fields. An unchanged prior same-source record cannot satisfy partial-receipt recovery. Invalid or conflicting evidence and legacy incomplete receipts without attempt provenance require explicit reconciliation; they do not upload or silently choose an older build.

Completed matching receipts recover without rebuilding. Recovery preserves the original receipt and durably selects the build and a separate `archive-recovered-*.json` path before writing that snapshot. A crash during snapshot publication resumes the selected identity even if the helper record later changes. The helper must verify the archive's actual build and version under its lock before upload. Recovery checks status first; a build already processing or available does not trigger an upload.

The updater requires a merged PR and successful `contracts` and `native-kanban` check runs from the GitHub Actions app (ID `15368`, slug `github-actions`) on the exact merge SHA. PR status rollups do not satisfy this gate. Other failing or pending checks and truncated check/status responses still block deployment.

It uses Coconut's existing package installer, verifies installed file hashes, and checks local TLS health. The update changes Coconut only. The installer runs the Hermes contract check of the new code against the installed Hermes, and restores the earlier code when it breaks a Hermes that worked before. A healthy matching installation is not deployed again on the next interval. The installer preserves runtime data and controls deployment rollback. A lock prevents overlapping automatic runs.

The updater stores its progress beside the config in `automatic-state.json`. A changed iOS input hash schedules an archive and TestFlight upload. Backend-only changes do not create another build. Apple processing does not prevent later backend deployments. A missing initial iOS hash creates one baseline build; an operator may seed a verified existing build and its exact input hash.

Failures remain in the launch agent's error log. The next interval retries the same operation. A failed check prevents deployment. An Apple rejection requires investigation; the updater does not generate replacement builds without a new source change.
