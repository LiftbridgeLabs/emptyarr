# emptyarr build test inventory

This repository-only document describes the validations that run before every
Docker image publication. Markdown files are excluded by `.dockerignore`, so
this file is not included in the published Docker image.

The authoritative workflow is `.github/workflows/docker-publish.yml`. A failed
step stops the build before Docker Hub login and publication.

## Unit and safety suite

Command:

```shell
python -m unittest discover -s tests -v
```

### Configuration precedence

- `test_empty_environment_values_do_not_override_file_settings` — empty
  environment variables must not erase saved settings.
- `test_session_key_is_generated_once_and_persisted` — the Flask session key
  must remain stable across reloads.

### Live configuration

- `test_duplicate_plex_machine_identifier_is_rejected` — the same Plex server
  cannot be imported twice under different names.
- `test_invalid_cron_is_rejected_before_apply` — invalid schedules never reach
  the live scheduler.
- `test_library_without_safety_path_is_rejected` — every monitored library
  requires at least one filesystem safety path.
- `test_live_apply_reconciles_jobs_and_removed_libraries` — live settings
  changes update jobs and remove stale dashboard state without a restart.

### Plex authentication and discovery

- `test_connections_prefer_local_non_relay_and_parse_string_booleans` — Plex
  discovery prefers direct local connections and correctly handles Plex boolean
  strings.

### Plex inventory behavior

- `test_tv_count_uses_episode_type` — TV safety ratios compare disk files with
  Plex episode counts rather than show counts.
- `test_count_failure_is_not_reported_as_zero` — a failed Plex count is unknown,
  not an apparently safe empty library.
- `test_trash_inventory_failure_is_explicit` — incomplete trash inventory
  fails closed.
- `test_trash_inventory_keeps_same_title_with_distinct_plex_ids` — separate Plex
  items with the same title remain distinct in safety snapshots.

### Trash protection and destructive workflow

- `test_missing_plex_count_fails_closed` — an unavailable Plex count blocks
  emptying.
- `test_debrid_mount_passes_when_discovered_mount_is_populated` — a populated
  discovered debrid mount passes.
- `test_debrid_mount_fails_when_discovered_mount_is_empty` — an empty underlying
  debrid mount fails.
- `test_provider_checks_receive_live_config` — provider checks use current
  settings.
- `test_overlapping_library_run_is_skipped` — scheduled and manual runs cannot
  overlap for the same library.
- `test_failed_health_check_never_empties_trash` — a filesystem check failure
  prevents the destructive call.
- `test_unreachable_plex_never_empties_trash` — Plex reachability failure
  prevents inventory and deletion.
- `test_missing_count_never_empties_trash` — runner orchestration preserves the
  fail-closed count policy.
- `test_failed_provider_check_never_empties_trash` — configured provider failure
  prevents deletion.
- `test_missing_section_never_empties_trash` — an unresolved Plex library never
  reaches deletion.
- `test_failed_initial_inventory_never_empties_trash` — the first inventory must
  succeed.
- `test_dry_run_never_empties_trash` — dry runs never call Plex Empty Trash.
- `test_clean_bundles_failure_never_empties_trash` — an enabled Clean Bundles
  failure stops the run.
- `test_paused_scheduling_never_empties_trash` — paused scheduled work remains
  paused.
- `test_manual_run_can_bypass_paused_scheduler` — explicitly requested manual
  work remains available while scheduling is paused.
- `test_empty_snapshot_does_not_call_empty_trash` — empty trash does not issue a
  needless destructive request.
- `test_failed_final_preflight_never_empties_trash` — safety checks are repeated
  immediately before deletion.
- `test_changed_trash_snapshot_never_empties_trash` — trash added or removed
  after the initial inventory cancels the run.
- `test_deletion_limit_never_empties_oversized_snapshot` — the absolute item
  limit is enforced.
- `test_percentage_limit_never_empties_oversized_snapshot` — the active-library
  percentage limit is enforced.
- `test_successful_run_has_one_destructive_call` — a fully valid run contains
  exactly one Plex Empty Trash request.

### Web and API security

- `test_ui_renders_with_security_headers` — the UI returns the expected CSP and
  clickjacking protections.
- `test_state_change_requires_csrf_for_browser_session` — browser mutations
  require the session CSRF token.
- `test_invalid_api_token_does_not_bypass_csrf` — merely supplying an API token
  header does not bypass CSRF.
- `test_valid_api_token_authenticates_without_csrf` — a verified independent
  bearer token supports non-browser automation.
- `test_password_hash_is_not_an_api_token` — login password hashes are not API
  credentials.
- `test_generated_api_token_is_revealed_once` — generated tokens are returned
  only at creation.
- `test_generated_api_token_persists_only_its_hash` — plaintext API tokens never
  reach `config.yml`.
- `test_metadata_address_is_rejected` — known cloud metadata addresses are
  rejected as Plex targets.
- `test_browse_opens_at_allowed_roots_and_stays_inside_them` — filesystem
  browsing cannot escape configured roots.

## Static and rendered-code validation

- `python -m compileall -q app.py src tests` compiles every Python source file.
- The Flask index is rendered through the test client, every inline script is
  extracted, and Node.js parses the resulting JavaScript with `new Function`.
  This catches errors that only appear after Jinja template rendering.

## Configuration validation

- PyYAML parses `data/config.yml.example`.
- Python's XML parser loads `unraid/emptyarr.xml`.
- `docker compose config --quiet` validates the Compose model.

## Container validation and publication

After all tests pass, the build uses the production `Dockerfile` and explicit
allowlisted `COPY` instructions. The workflow publishes:

- `liftbridgelabs/emptyarr:latest`
- `liftbridgelabs/emptyarr:<full-git-commit-sha>`

The Docker build context excludes repository metadata, tests, local
configuration, runtime data, logs, editor files, and Markdown documentation.
