---
name: velvet-development
description: Plan, implement, verify, release, and hand off changes to the Velvet local video library. Use for Velvet performance, UI, scraping, storage organization, regressions, and authorized service maintenance in a Velvet checkout.
---

# Velvet development

Locate the Velvet checkout. Check Git status, VERSION, and current context in the checkout's docs/HANDOFF.md. For runtime work, verify the served source path, service identity, and health. Develop in a separate checkout from the serving copy, since separate VELVET_DATA does not isolate static assets. Keep existing edits and use synthetic media, an independent data directory, and a free port for verification.

## Route by task

Resolve the following repository paths from the Velvet checkout, including when this skill is installed separately.

| Task | Read in the checkout |
|---|---|
| Plan or change scope | AGENTS.md, docs/PLAN-INDEX.md, the linked version requirements, and docs/DELIVERY-STATUS.md |
| Implement or review | AGENTS.md, docs/development/README.md, and the relevant module, API, or data contract |
| Measure or accept | docs/development/TESTING.md and the active requirement's baseline and acceptance criteria |
| Change organization, recovery, or restore | docs/development/ORGANIZATION-API.md, docs/development/DATA.md, docs/ALPHA6-OPERATIONS.md, and the relevant ADR |
| Release or maintain a service | docs/RELEASING.md and docs/DEPLOYMENT.md |

Use the current requested version and the linked requirements. Historical audits and release notes provide context and evidence. Record requirement ID, change, test conditions, results, and gaps. Update the authoritative doc and evidence links. Use the [handoff template](references/handoff-template.md) when transferring work.

## Velvet invariants

- Treat real source media as read-only for development, scanning and planning. Actual organization requires authorization for the specific execution plan. Preserve approved scope, file identity and proof, durable checkpoints, and reconcile protection. Restore of the database requires separate reconciliation of physical file operations.
- Preserve manual locks and decisions, ratings, playback progress, exclusions, and privacy masking. Folder collections are presentation groups distinct from movie, version, and part identity. Automatic OCR scores are heuristics.
- Verify changes through the public entry point with synthetic data. Inspect code to read constants, since importing app.py initializes data. New UI assets must agree across UI_ASSETS, HTML and Docker packaging. Extend existing queue and navigation lifecycles when changing tasks or views.
- Keep private paths, real images, media, runtime data, OCR output, and credentials out of commits and reports. Check relevant tests and actual browser behavior. Use release.py --check for tracked or staged public files and run tests separately.
- Report implemented, accepted, merged, released, and deployed as separate facts. Follow existing user authorization for publishing and runtime operations. For an authorized upgrade, verify the process, drain work safely, back up data, preserve automation settings, and verify the new health response.
