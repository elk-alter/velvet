---
name: velvet-development
description: Continue development of the Velvet local video library, including alpha.5 tasks, raw-media organization planning, regressions and development handoffs. Use in a Velvet checkout; not for unrelated media apps or organizing actual source files without an explicit execution request.
---

# Velvet development relay

Locate the intended checkout from the user's workspace, then read its AGENTS.md. Use git status and recent commits to establish reality; do not assume the installed skill directory is the repository. If no checkout is available, ask for its location rather than cloning or modifying an unrelated project.

## Select context by task

Read docs/development/README.md and docs/PLAN-INDEX.md. For alpha.5 work, select a bounded A5-ID from docs/ALPHA5-REQUIREMENTS.md; for original-media organization select an ORG-ID from docs/EPIC-RAW-MEDIA-ORGANIZATION.md. Consult docs/DELIVERY-STATUS.md before claiming a story is new or completed. Requirement acceptance criteria outrank abbreviated status summaries. Read MODULES.md for affected ownership and DATA.md for any schema, queue, exclusion or restore change.

Verify VERSION, release records and HANDOFF instead of assuming the checked-out code is deployed. Existing buttons and tests do not prove story acceptance. The raw-media Epic currently provides read-only inventory/preview; the copy pilot is deferred, as are moving/renaming/source disposal. A request to plan or implement a planner is not authorization to reorganize actual disks. Do not silently weaken current source-read-only constraints.

## Implement and verify

Keep the requested scope and preserve existing user changes. Tie the change to its story and acceptance criteria, inspect dependencies, and implement a reviewable slice. Complex irreversible/data decisions need an ADR under docs/adr; this is documentation, not an invented approval gate.

Preserve manual metadata locks, relationship corrections, quality decisions, exclusions, playback preferences and privacy masking. Folder collection display is distinct from movie/version/part identity. Metadata, pictures, OCR and scan queues currently have different lifecycle/storage: inspect their actual interaction before extending them. Do not create another isolated boolean-based task subsystem or layer another global wrapper without considering a module boundary.

Use independent VELVET_DATA, synthetic media and mock services. Do not import app.py merely to inspect constants: importing initializes data. Follow TESTING.md for relevant tests; release.py --check does not run backend or browser tests and only sees tracked files. New assets need UI_ASSETS, HTML and Docker packaging agreement. OCR scores are heuristic, not calibrated probabilities.

Update the authoritative requirement/status and affected usage/architecture docs. Record actual test commands and evidence, failures and unverified scenarios. Never put private paths, media, OCR output, runtime databases, credentials or real thumbnails into commits, reports or public examples.

## Handoff and delivery

Use [handoff template](references/handoff-template.md) when work stops or changes hands. Update docs/HANDOFF.md with concise current context; update DELIVERY-STATUS only for IDs actually assessed. Never mark acceptance passed based on code presence alone.

Distinguish implemented, committed, pushed, released and deployed. A coding request does not automatically authorize release, GitHub messages or production process changes. Honor authorization already present in the conversation without asking again. If restart is in scope, verify the actual process and task idleness; a TIME_WAIT connection/PID0 is not the service. Preserve original automation switches and verify HTTP health after restart. Do not terminate unrelated processes.

For a release, read docs/RELEASING.md, test the extracted Git archive, and disclose remaining gaps. CI is currently an example, not enforced branch protection. Do not claim all NAS/platform/encoding scenarios are validated.
