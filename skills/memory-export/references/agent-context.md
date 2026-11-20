# Agent context and project instructions inside skills

Use this branch for a Dots/Duet context transfer or when a selected skill depends on project instructions. Collect supplied user-owned context; do not inspect private accounts or expand source access without the user's scope and actual host tools. Do not reduce the task to a memory summary.

## Account for context without another questionnaire

Reuse the conversation, provided exports/settings snapshots and known choices. Check only the actual source needed next. The model organizes the snapshot; the offline code validates and copies it. A template is not an account scan. Record inaccessible and unchecked categories instead of pretending they are empty or repeatedly asking the user for all 15 categories.

| Category ID | Keep when relevant and actually available |
| --- | --- |
| `account_instructions` | User-authored account instructions, separately from facts in memory |
| `agent_instructions` | Instructions for the particular dot/agent and their scope |
| `agent_rules` | User-defined action/approval boundaries; not credentials or host permissions |
| `saved_memory` | Accessible saved facts and preferences with their source |
| `agent_notes` | The agent's separately accessible persistent notes |
| `projects` | Project identity, goal, status and important context |
| `project_instructions` | Exact user-authored project rules and source project ID |
| `skills` | Observed skill inventory and dependencies; originals use the separate collector |
| `responsibilities` | Ongoing responsibilities, expected outcomes and boundaries |
| `active_tasks` | Current work, next action, waiting condition and result locations |
| `recurring_tasks` | Task instructions, schedule, time zone, trigger, enabled state and end conditions |
| `integrations` | Account/resource identity and observed access; no transferable authorization |
| `channels_and_destinations` | Where results and notices should go, with audience and scope |
| `artifacts` | Available outputs, their provenance and links to work |
| `conversations` | Selected supplied conversation context; not an inferred full account history |

Keep explicit text and structured values intact. Do not invent a schedule, time zone, dependency, destination or permission that is absent. Preserve unknowns and ask one useful question only where the next step needs it. A user-selected narrow scope can explain excluded categories; do not invent exclusions or decide them from silence.

## Snapshot and executable collection

```text
python scripts/memory_tool.py context-template --platform dots --source context-source.json --out context-snapshot.json
python scripts/memory_tool.py context-collect context-snapshot.json --out collected-context --accept
```

Use `duet` for the other supported platform. Fill the snapshot from actual provided sources between these commands. The source object is `{platform: dots|duet, account_id: null|string, workspace_id: null|string, observed_at: string, evidence: string}`. `context-template` accepts the platform in this object or adds the CLI platform; a mismatch is invalid. Do not guess unknown account/workspace IDs or merge unrelated agents by display name.

The snapshot has `format: portable-agent-context`, `schema_version: 1`, `source` and `categories`. Each category is `{id,status,evidence,source_refs,items,excluded_by_user}`.

| Status | Meaning and required basis |
| --- | --- |
| `pending` | Not checked or not yet supplied; never report as absent |
| `provided` | Actual items plus nonempty evidence and source references |
| `unavailable` | Known access/source gap, with the actual reason |
| `not_found` | A relevant check actually ran and found nothing, with evidence and source references; not merely a missing file |
| `excluded` | A real user scope decision, with `excluded_by_user: true` and its evidence |

An item is `{id,title,kind,scope,value,source_ref}` with optional `project_id` and `observed_at`. `value` is a non-null JSON value retained without summarization. Use a plain string for a verbatim instruction; objects/lists retain structure, not the original JSON file's byte formatting. Each item's source reference belongs to its category's observed sources.

Supported kinds: instructions/rules categories use `rule`; saved memory uses `profile|contact|account|preference|rule|resource|idea`; agent notes use `profile|idea|resource`; projects use `project`; skills use `skill`; responsibilities/active tasks use `workflow`; recurring tasks use `automation`; integrations use `integration|account`; channels/destinations use `resource|account`; artifacts use `artifact`; conversations use `resource`.

A fictional project-instruction category example, to illustrate schema only:

```json
{
  "id": "project_instructions",
  "status": "provided",
  "evidence": "Fictional user supplied this project's instruction text.",
  "source_refs": ["fixture:project-settings"],
  "items": [{
    "id": "project-table-rule",
    "title": "Project table format",
    "kind": "rule",
    "scope": "project:source-project-1",
    "project_id": "source-project-1",
    "value": "Use columns Decision, Source, Next step. Keep unresolved questions visible.",
    "source_ref": "fixture:project-settings"
  }],
  "excluded_by_user": false
}
```

A project instruction requires `project_id` and exactly `scope: project:<source_project_id>`. Include the actual project as a `projects` item when supplied, with matching project scope. Collection links to a real provided project record where possible; it does not fabricate a project just to complete a link. Missing project material remains a gap to resolve before binding a skill to it.

Collection writes `assets/context/source.json`, pending `memory.json`, and `context-report.json` / `context-report.md`. The saved source is a serialized snapshot with unchanged values, not a byte-identical copy of the original JSON file. Missing categories appear as pending in the report; the input snapshot is not silently filled with invented observations. Collected records retain `source_category`, `source_item_id` and source project identity where applicable. Use actual returned record IDs; they are not necessarily the original source item IDs.

The report lists `unmet_categories`, `unresolved_projects` and per-category statuses. `all_categories_assessed` only means none remain pending; `all_requested_categories_available` only describes the supplied category claims. `complete_account_export` remains false: even all 15 categories filled do not establish exhaustive platform access. Unknown account/workspace identity is reported and kept separate rather than silently merged.

Every collected record starts pending with unknown currency. Collection does not endorse its meaning or make it a current instruction. Use the existing evidence/review flow for profile and rules, and preserve known user decisions while combining records. `skills` items in the context snapshot describe the inventory; use [instructions-and-skills.md](instructions-and-skills.md) to obtain original skill files rather than treating a context mention as a full skill.

## Make project instructions reachable from a selected skill

A `belongs_to` graph link or nearby wiki page does not guarantee that the receiving agent loads project instructions when using the skill. First establish which exact project rules belong to that skill. Inspect supplied original instructions and the user's stated workflow; do not bind every project rule to every skill.

Preserve the original text, source and project scope. If a rule is intended for another project/agent, review the explicit scope mapping rather than broadening it to global. User-owned project instructions can be exported. Hidden provider/system prompts are not source material for this operation.

Use the actual IDs from the combined memory, after reviewing the selected project/rules and the skill's source. A fictional bindings file has this shape:

```json
{
  "format": "portable-skill-context-bindings",
  "schema_version": 1,
  "items": [{
    "skill_id": "actual-skill-record-id",
    "project_id": "actual-project-record-id",
    "instruction_ids": ["actual-project-rule-record-id"]
  }]
}
```

```text
python scripts/memory_tool.py skill-bind-context reviewed-memory.json --assets selected-assets --bindings bindings.json --out bound-export --accept
```

Replace example IDs with observed memory record IDs. The project and all bound rules must share the same explicit `project:` scope; a rule's `source_project_id`, where present, must agree. Missing skill originals cannot be replaced with a generated fake skill. The command works on an exported copy, preserves the original SKILL.md, adds actual instruction reference files and appends a reference index to that copy's SKILL.md. Plain text stays verbatim; structured rule values are rendered as JSON without semantic rewriting.

The output is `bound-export/memory.json`, `bound-export/assets/` and `bound-export/binding-report.json`. The exported skill's original entry is preserved at `references/portable-context/SKILL.original.md`, with rule reference files alongside it. A partial original snapshot may be bound if the actual SKILL.md is present; its partial status and exclusions remain, and missing dependencies are not repaired by the binding.

Read the binding report and output memory. The changed skill requires fresh content-bound review; do not restore an old approval by copying its hash. Export that memory and its sibling assets together. Bound project/rule records and matching reference files are dependencies of the export: do not exclude or edit them afterwards and expect the prior binding to remain valid. To change a bound rule, update and review the source records and rebind from the original skill bundle into a fresh output. At import, preserve those exact dependencies through the merge; do not pair the incoming skill with a conflicting older rule and claim its context was restored. Resolve the conflict or leave the dependent skill unapplied.

Binding keeps the actual source project ID and a `belongs_to` link to the chosen project. Export/package inspection also checks the entire bound skill subtree against its snapshot hash: changing a script or configuration file needs a fresh bundle and review, not just an unchanged rule reference. Import verifies every bound skill retained in the merged memory against the incoming assets. `--current` supplies JSON, not the previous skill files; a new partial package that omits files needed by a retained bound skill is rejected. Prepare a complete agreed file set for that merge. The previous workspace is not modified by the rejection.

The receiving agent still checks compatibility, dependencies, intended enabled state and actual installation. Reading the reference index does not authorize running imported code or resume tasks. A portable copy is not proof that the host will load the skill or configure native project instructions.

## Platform distinctions and honest coverage

Dots custom instructions differ from ChatGPT custom instructions; dot notes differ from ChatGPT saved memory. Responsibilities and active/recurring tasks also need their own context, including result destinations. Claude account instructions, project instructions and skills serve different scopes; Duet uses Claude account memory/skills/apps, which does not imply an export API or a separate independent catalog.

Official sources checked 2026-11-20: [Dot controls](https://learn.chatgpt.com/docs/dots/controls), [Dot tasks and memory](https://learn.chatgpt.com/docs/dots/tasks-and-memory), [Claude personalization](https://support.claude.com/en/articles/10185728-understanding-claude-s-personalization-features), [Claude Duet](https://claude.com/product/claude-duet). Actual access must be verified in this session. All category statuses are assertions grounded in supplied observations, not proof of exhaustive account access. No snapshot or state file creates a background watcher, live schedule, OAuth session or native setup action.

