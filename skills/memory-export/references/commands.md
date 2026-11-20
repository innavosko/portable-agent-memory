# Export commands

Run from the installed skill directory, using an available Python 3.10+ interpreter. No pip dependencies are required. Use the bundled code as shipped; do not rewrite it from memory. The host's execution environment provides Python, not the user's browser itself.

For existing installed/available skills, use `skill-discover` and `skill-collect` through [instructions-and-skills.md](instructions-and-skills.md). This preserves selected originals and their inventory independently of drafting new skills from works. Add its reviewed records and selected assets to the chosen export; a supplied-folder inventory is not a full account export.

For a supplied Dots/Duet context snapshot, use `context-template` / `context-collect` in [agent-context.md](agent-context.md). For selected project instructions needed by a skill, use `skill-bind-context`, review the changed skill record and export its resulting memory/assets together. These operations do not fetch account contents or apply native settings.

Use [common-profile.md](common-profile.md) for the required common review, exact semantic schemas, source cards and command sequence. One coordinator can accept raw materials from several agents; source-side skill installation is optional. Source capture and combined readiness are different results.

```text
python scripts/memory_tool.py --help
python scripts/memory_tool.py ingest source.zip --out parsed-source
python scripts/memory_tool.py common-view --store common.sqlite
python scripts/memory_tool.py common-result --store common.sqlite --out common-memory.json --report common-report.json
python scripts/memory_tool.py export common-memory.json --common-store common.sqlite --out memory-package.zip
python scripts/memory_tool.py inspect memory-package.zip
```

`ingest` normalizes provided conversation files; it does not infer a profile. Read its report and conversations before extracting records. Output directories/files must be new where the command refuses overwrite.

This is an outline after completing the common review, not a sequence to run blindly. Read [profile-review.md](profile-review.md) for source-evidence schemas and user-event payloads. Complete both the source-support check and the cross-source meaning check; record real user decisions through the common store. When a new source or changed capture arrives, refresh that review and reuse unchanged confirmations instead of restarting the whole interview.

The lower-level `review`, `confirm` and `profile-*` commands remain available for preparation. They do not replace the required common export gate. Never treat a generated pending decisions template as user approval. Editing a record after confirmation requires fresh review; binding project instructions into a skill also changes its exported record, so register that result before the final common check.

Use `export ... --common-store common.sqlite --assets selected-assets` only for a folder containing exactly the selected artifacts, source files or drafted skills. Keep private sources out of the program repository. Do not silently bundle the original archive in a profile-only export.

For standard folder delivery, follow [handoff.md](handoff.md). Reuse the chosen sending and receiving storage; the services can be the same or different. Report a successful upload only after an actual authorized host tool verifies it, then let the user copy the complete version folder and initiate receiving. The Python program does not upload to clouds or obtain authorization. Offer download/manual upload where necessary. Include the profile report among selected handoff materials and explain its coverage.

Use the migration flow in [journey.md](journey.md) for multi-source collection. Legacy `transfer-create`, `transfer-event` and `transfer-next` describe one transfer's events. Neither state mechanism creates a cloud bridge, an email watcher or an external notification.
