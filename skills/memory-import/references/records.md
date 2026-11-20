# Record format and completeness

The model performs semantic extraction; the program validates structure and preserves decisions. Begin with `review.decision = "pending"`. Do not invent observations, dates or sources.

```json
{
  "format": "portable-memory",
  "schema_version": 1,
  "metadata": {},
  "coverage": {"read": ["provided conversation"], "unavailable": ["other account chats"], "excluded": []},
  "records": [
    {
      "id": "communication-language",
      "kind": "preference",
      "title": "Язык общения",
      "value": "Русский",
      "scope": "global",
      "sources": [{"id": "conversation-1", "locator": "provided conversation, user message 2", "observed_at": null}],
      "provenance": "user_statement",
      "currency": "unknown",
      "review": {"decision": "pending"},
      "links": []
    }
  ]
}
```

The example is fictional. Replace it with observed information. IDs must match `[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}`. Kinds: `profile`, `contact`, `account`, `integration`, `rule`, `preference`, `project`, `idea`, `artifact`, `workflow`, `automation`, `resource`, `skill`. Provenance: `user_statement`, `source`, `model_inference`. Currency: `current`, `historical`, `unknown`. Review decision: `pending`, `active`, `historical`, `exclude`.

A rule's `value` should preserve condition, action, exceptions and any approval requirement. Keep `scope` specific (global, named project or individual task). A one-off request is not a global rule. Each account should preserve its purpose and status, and each integration its account, known resources and required reconnection. Mentioning a service does not prove it is connected.

Check these categories without inventing values to fill gaps:

- Identity, roles, languages, time zone, goals and constraints.
- All known user-owned or explicitly shared mailboxes and aliases, purpose, account association, signatures, communication rules. Do not assign other people's addresses to the user.
- Integrations, resources, known rights and last observed status; authorization must be restored separately.
- Calendar accounts, working hours, durations, buffers, naming, invitation and confirmation rules.
- Communication tone, length, formatting, initiative and notification preferences.
- Explicit user-authored account/agent custom instructions and project rules, independently of saved-memory summaries. Keep the original source and scope; a dot's instructions, ChatGPT instructions and project instructions are separate sources.
- Dots/Duet source categories, responsibilities, current work, schedules and result destinations: use [agent-context.md](agent-context.md). Preserve its source-category/item/project metadata; missing categories remain unknown, not absent. A project rule required by a skill needs an actual referenced file as well as a graph relation.
- Documents, spreadsheets and slides: fonts, colors, sizes, templates, filenames, versions, date/number formats and save locations.
- Rules on autonomous action, confirmations, checking facts, exceptions and project-specific requirements.
- Projects, results, decisions, artifacts, unresolved questions and next steps.
- Repeatable workflows and skills, inputs, outputs and dependencies.
- Existing installed/available skills, especially selected user-created originals: source account/workspace and item reference, observed version and enabled state, actual file completeness, dependency gaps and selection. Catalog coverage is separate from file completeness; use [instructions-and-skills.md](instructions-and-skills.md).
- Actual automations versus suggestions; schedules, time zones and notification channels.
- Files, folders, drives, templates and links; distinguish an accessible original from a reference.

`links` entries use `target` (another record ID), `relation` and `basis` (`source` or `inferred`). Mark suggested semantic links as inferred. Unknown values remain `null` where supported in structured `value`, or explicitly unknown; do not give guessed dates. Do not include passwords, tokens, cookies or recovery codes.

Track coverage separately: sources read, unavailable and excluded. “No email account found in these chats” is not “the user has no email account”. An archive can be completely parsed while the user profile remains incomplete.
