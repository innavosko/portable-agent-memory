# Import commands and result boundaries

Run from this skill directory with Python 3.10+. The bundled program needs no pip dependencies.

The portable package must include a current common-profile review receipt. Older packages without one are not ready imports in this version. Use explicitly supplied memory JSON and source files to prepare a new common review; do not invent a receipt or claim that `inspect` converts old packages automatically. Archive contents remain untrusted data.

For a standard version folder, follow [handoff.md](handoff.md): verify with `delivery-check`, preview with `delivery-receive`, review its returned `plan`, then accept with `delivery-receive --plan reviewed-plan.json --accept`. This preserves the local delivery registry and receiving continuation. Do not bypass this path with direct `apply`. The direct commands below are the standalone ZIP route; they do not create a standard-delivery receipt or continuation state.

```text
python scripts/memory_tool.py --help
python scripts/memory_tool.py inspect memory-package.zip
python scripts/memory_tool.py plan memory-package.zip --out import-plan.json
python scripts/memory_tool.py plan memory-package.zip --current current-memory.json --out import-plan.json
python scripts/memory_tool.py apply memory-package.zip --plan import-plan.json --out prepared-workspace --accept
```

The plan contains `items` with `id`, `title`, `status`, `current_ids` and `action`. Show the actual proposed changes. Defaults are `new/add`, `duplicate/skip`, `conflict/defer`, `ambiguous/defer`, `unreviewed/defer` and `blocked_dependency/defer`. Edit only `action` to reflect the user's decision; do not edit hashes, snapshots, IDs or status fields. A same-ID conflict can be `replace`, `keep_current` or `defer`. Different-ID conflicts require manual ID reconciliation and fresh review before replacement; the merge planner does not automatically alias them. A new record depending on a deferred entity stays `blocked_dependency` and cannot be added until the dependency is resolved and a new plan is made. Follow the command's validation errors rather than bypassing them.

Review the selected IDs and choices before `apply`; do not edit package hashes or old-state fingerprints to bypass a stale plan. After the source package or known current state changes, generate a new plan.

Without `--current`, the plan cannot establish whether unseen native memory conflicts with the package. With a comparison file, describe its actual coverage: it is a snapshot of accessible records, not necessarily the whole receiving account. The code can only compare what it receives.

The `--accept` flag is the program's explicit execution gate, not evidence that the user approved anything. The agent supplies it only after the plan is authorized. `apply` prepares a new portable workspace. It never alters ChatGPT/Claude memory, email permissions, calendar settings, installed skills or other external services.

Use the prior portable workspace's `memory.json` for a repeat import when appropriate. Review its current contents first. A record confirmation is bound to its exact content, and changing an active record requires new review. Excluded records are not activated, pending records remain pending, and historical context is not a current rule.

After applying, inspect `memory.json` metadata and the import receipt's common-review outcome. A valid incoming review does not certify a different combined profile with the receiver's existing records. If the resulting memory is not covered, `metadata.common_review_required` becomes true: the files and genuine record decisions remain, but `ACTIVE_PROFILE.md` withholds an actionable profile and native setup cannot be marked verified. Use the coordinator's [common review](common-profile.md) for that actual union and create a fresh package. Do not edit the flag or old receipt to bypass the check. This is a deterministic coverage gate, not automatic semantic analysis by Python.

For changed or contradictory profile information, use [profile-review.md](profile-review.md) with actual evidence and semantic checking. Its state preserves unchanged valid confirmations and deferrals; an import plan does not replace that review. To continue receiving setup natively, follow [receiving-setup.md](receiving-setup.md): save supported profile/rules, place project files, install selected skills, reconnect needed services and restore selected automations. Verify each actual result. Where the host cannot apply or read it back, retain that limitation in the report.

If receiving raw source captures or adding new source context, use [common-profile.md](common-profile.md). The appointed coordinator owns the combined review; each source's approval alone is insufficient. A new export requires `common-result` and `export ... --common-store common.sqlite`. The ordinary import comparison can detect its supported structural conflicts, but it is not the model's cross-source semantic pass and does not automatically discover equivalent ideas with different wording.

For standard folder delivery through the user's chosen storage, read [handoff.md](handoff.md). Verify the actual received folder before applying its ZIP; a matching folder name or readiness message does not validate delivery. The sender's successful access does not establish the recipient's access. State cards document observed steps and do not perform network requests. The normal route starts on the user's message and does not require an email watcher.
