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

The upload command must check Apple for an existing build before each attempt. It must refuse a duplicate upload. An archive receipt contains `source`, `source_hash`, and `build`. The source hash uses the same input paths and SHA-256 calculation as `ios_hash()` in the updater.


The updater requires a merged PR with completed, successful checks. It uses Coconut's existing package installer, verifies installed file hashes, and checks local TLS health. The installer preserves runtime data and controls deployment rollback. A lock prevents overlapping automatic runs.

The updater stores its progress beside the config in `automatic-state.json`. A changed iOS input hash schedules an archive and TestFlight upload. Backend-only changes do not create another build. Apple processing does not prevent later backend deployments. A missing initial iOS hash creates one baseline build; an operator may seed a verified existing build and its exact input hash.

Failures remain in the launch agent's error log. The next interval retries the same operation. A failed check prevents deployment. An Apple rejection requires investigation; the updater does not generate replacement builds without a new source change.
