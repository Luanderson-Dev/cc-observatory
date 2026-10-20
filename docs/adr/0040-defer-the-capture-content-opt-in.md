---
status: accepted
date: 2026-10-20
decision-makers: Luanderson-Dev (maintainer)
---

# Defer the capture-content opt-in

## Context and problem statement

[ADR 0006](0006-privacy-metadata-only-by-default.md) stores metadata only by default and
plans an explicit opt-in, `ccobs setup --capture-content`, that turns content on in
Claude Code's settings, the collector and the app, leaving to milestone 4 how the CLI on
the host switches the containers. `README.md`, `docs/app.md` and `docs/stack.md`
promised it with `ccobs setup` in v0.5.0. The opt-in is more than a flag:

- the collector drops content-bearing telemetry while the opt-in is off
  ([ADR 0021](0021-collector-privacy-filters.md)), so the opt-in has to change the
  running collector's pipelines, and the app's per-event allowlist
  ([ADR 0023](0023-per-event-allowlist-of-hook-metadata.md)) has to grow content fields;
- Loki limits each entry's structured metadata to 64 KB and rejects an entry over it,
  so long content must not go into structured metadata
  ([ADR 0015](0015-loki-through-its-otlp-endpoint.md), lines 133 to 139): a content
  path needs its own storage design, with its own retention;
- [ADR 0036](0036-the-telemetry-env-block-that-ccobs-setup-writes.md) keeps every
  content variable (`CONTENT_VARIABLES`: the `OTEL_LOG_*` flags and the detailed beta
  tracing pair) out of the block setup writes, and `ccobs doctor` warns when one is
  enabled ([ADR 0039](0039-ccobs-doctor-and-status-checks-sources-and-exit-codes.md),
  `content-variables`).

Milestone 4 ships `setup`, `uninstall`, `doctor` and `status` without any of that. Does
v0.5.0 include the opt-in, and what do the docs promise?

## Decision drivers

- Privacy first: content is the most sensitive and the bulkiest data (ADR 0006).
- No promise in the docs that the code does not keep.
- A content path that stores content safely: storage, size limits, retention and what
  the UI may show, decided together rather than one flag at a time.
- Ship the commands milestone 4 planned without waiting for that design.

## Considered options

- **(1) Ship `--capture-content` in v0.5.0.** Keeps the promise, but needs the storage
  design ADR 0015 calls for, a collector change switched from the host, content fields
  in the app and their retention, all inside one milestone.
- **(2) Ship v0.5.0 metadata only and defer the opt-in to its own ADR.** The commands
  ship now; content waits for a design of its own.
- **(3) Drop content capture altogether.** Simplest, but reverses ADR 0006's opt-in,
  which a user may still want.

## Decision outcome

Chosen option: "(2) ship v0.5.0 metadata only and defer the opt-in to its own ADR",
because it ships the milestone's commands with the privacy default intact and leaves
content to a design that covers storage, retention and the collector together; (1)
would rush that design, and (3) takes a choice away from users that ADR 0006 keeps.

- v0.5.0 stores metadata only. `ccobs setup` has no `--capture-content` option and never
  writes a content variable (ADR 0036's `CONTENT_VARIABLES`).
- The opt-in comes later, with a new ADR that decides where content is stored (not in
  Loki's structured metadata, ADR 0015), its retention, how the collector's content
  filters (ADR 0021) are switched off from the host, and which fields the app's
  allowlist adds (ADR 0023). It is not scheduled in any milestone yet.
- ADR 0006 stays in force: metadata only by default, with content capture as an opt-in
  once that ADR exists. The open items it, ADR 0015, ADR 0021 and ADR 0023 left to
  milestone 4 move to that ADR.

### Consequences

- Good, because the docs promise nothing about content that the code does not do: they
  say v0.5.0 is metadata only and link here.
- Good, because a user who sets an `OTEL_LOG_*` flag by hand still stores no content:
  the collector keeps dropping content-bearing telemetry (ADR 0021) and the app keeps
  its allowlist (ADR 0023).
- Good, because `ccobs doctor` warns about such a flag (`content-variables`), saying
  that cc-observatory does not support content capture yet.
- Bad, because there is no way to read prompts or tool output in cc-observatory until
  the new ADR is implemented.
- Neutral, because accepted ADRs that name milestone 4 for the opt-in (0006, 0015, 0021
  and 0023) are not rewritten; this ADR records that the item moved.

### Confirmation

- `tests/test_domain_telemetry_settings.py` checks that `telemetry_env()` never returns
  a `CONTENT_VARIABLES` key, and the `ccobs setup` tests that the block written equals
  it.
- `tests/test_domain_doctor.py` checks that `content-variables` warns on an enabling
  value.
- The collector config tests keep checking that content is dropped (ADR 0021).
- The `reviewer` subagent checks that `ccobs setup` gains no `--capture-content` option
  and that no doc promises the opt-in for a release.

## More information

- Related: ADRs 0006, 0015, 0021, 0023, 0036 and 0039; Claude Code's
  [monitoring docs](https://code.claude.com/docs/en/monitoring-usage) list what each
  `OTEL_LOG_*` variable exports.
- Revisit when a milestone plans the content opt-in; its ADR then extends ADR 0006 and
  this one is superseded.
