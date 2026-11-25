# Adapt the profile before exporting to GigaAgent

The aim is to preserve as much **supported, useful user context** as ordinary GigaAgent can store, without changing meaning to fit its memory rules. Its input remains only `import-instruction.md` and compliant profile Markdown; archives and skills remain unsupported. Follow [recipient-profiles.md](recipient-profiles.md) for the exact contract and native limits.

## Split meaning, not just files

For each selected source record, enumerate its meaningful clauses before deciding what enters memory. Separate independent facts, preferences, project rules/decisions, goals, exact links, temporary statuses, tasks and procedures. One source may support several output records with the same source IDs/hashes. Preserve the subject, project, conditions, factual status and meaningful dates in every resulting atom. Do not discard an entire record merely because one clause is temporary or describes a skill.

Synthetic examples below illustrate decisions; they are not facts about the current user:

| Mixed source | Adaptation |
| --- | --- |
| “I keep meeting notes in Markdown; I want to reorganize last week's notes.” | An evidenced recurring note-taking habit may be `user` or an explicit output preference `feedback`; the unfinished reorganization stays temporary. Do not turn the wish into an established habit. |
| “The move is paused; these regions are the long-term locations I prefer.” | The pause is a dated status. The explicitly long-term geographic preference can be assessed separately. If the source only names places under consideration, do not infer that the preference is durable; clarify only if material to the export. |
| “For project Cedar we decided to call its initial free period onboarding.” | A supported ongoing business-model decision can be a scoped `project` fact. It is not a global ban on other wording. Retain the project, decision and relevant date or conditions. |
| A user-authored project skill contains report layout rules, a working procedure and a source URL. | Inspect the accessible originals. A supported standing output preference, a scoped project rule and a precise useful reference may become separate facts **during common-profile review**. The skill file and executable procedure stay outside ready memory. |

A date alone does not make a fact temporary. A dated project decision or a genuinely stated long-term goal can remain relevant with its qualifications. A one-off deadline, current pause, incomplete certification or “I want to do X” must not automatically become a stable preference, completed achievement or durable goal. Do not remove dates, uncertainty or history to make a record look current. Follow actual corrections and confirmations; unknown relevance stays unresolved.

## Recover missing rules from evidence

Before asking the user to repeat rules, inspect the already available original instructions, skill files and project artifacts. Preserve traceable quotations, source and scope. Extract meaningful user-authored rules as separate supported source records in the common-profile workflow, then review the resulting union. A skill inventory entry alone is not evidence of its detailed rules. If originals are inaccessible, say what was not checked and ask only for the material needed to resolve the gap. Do not invent hidden rules or provider instructions, or use model recommendations as user preferences.

Only after the common profile covers these facts should its GigaAgent projection be updated. If the projection reveals new facts that were absent from the common profile, return to that review instead of bypassing it. A pending, historical or skill-type combined source cannot produce a ready clause merely by changing the target category: first create properly evidenced atomic source facts and obtain their appropriate current common review. The code permits one eligible source to contribute to several projection entries; the model, not Python, performs semantic splitting. Every resulting record still needs independent evidence and an allowed classification.

## Classify and verify every atom

Assign each meaning its actual disposition: `ready`, `clarify`, `temporary` or `reserve`, with a concrete reason. Review meaningful clauses that are excluded as carefully as those included. Keep a coverage table in working notes:

| Source clause and evidence | Output atom or exclusion | Scope/date/status retained | Why this disposition; where the material is preserved |
| --- | --- | --- | --- |
| Actual source IDs and quoted clause | Actual record name(s), or a stated omission | Specific qualifications | Actual source/backup path or an explicit unresolved preservation step |

Do not fill this with synthetic examples, generic “checked” notes or automatic approvals. In the projection's existing `quality.note`, describe the real atomization and coverage check: which mixed clauses were separated; where each meaningful clause ended up; evidence, scope, dates and decisions checked; missing rules, exclusions and source locations. This is a concrete review note, not a magic boolean or proof of semantic correctness. Use an independent checker when available; otherwise identify the same model's second pass honestly. Check every atom and the whole profile for meaning, duplicates and contradictions after rewriting.

Preserve valid user confirmations and deferrals. Splitting a sentence does not require re-interviewing the user about unchanged meanings. A changed meaning or unresolved contradiction needs a focused question; a fresh hash alone cannot renew approval.

Category limits are ceilings, not quotas. Do not invent entries, combine unrelated facts into a crowded record, move a fact to a false category, shorten away conditions, or disguise historical/temporary context to fit. Keep useful overflow in reserve. Do not export secrets, hidden provider instructions or facts the user asked to forget; report the class of omission without reproducing the excluded content.

## Show the result before final files

After the actual semantic review, create a preview from the current memory, common review and projection:

```text
python scripts/memory_tool.py giga-preview --memory common-memory.json --common-store common.sqlite --projection giga-projection.json --out giga-preview.json
```

The command saves a preview JSON and returns `{output, preview, native_memory_applied: false}`. **Display `preview.markdown` in the conversation before `giga-export`.** In the saved JSON the field is directly `markdown`. Include three understandable groups, adding a short evidence-based explanation where needed:

1. **Войдёт в память:** the actual adapted facts and their category, with relevant scope and conditions.
2. **Не войдёт:** each excluded meaning, its reason and where it is actually preserved. Name a real accessible source, previously created backup or working document. If preservation is not done, say so and identify the pending step; never invent a file or claim a proposed output already exists.
3. **Не хватает / нужно уточнить:** inaccessible originals, missing rules and unresolved decisions; say which require an answer and which are explicitly deferred. Do not repeat the entire profile interview.

The generated preview is bound to the reviewed source/projection and includes ready/excluded records, counts, limitations and split-source mappings. Its JSON does not prove that the user saw the preview, consented, or that native memory was written. The host must show it. If there is no unresolved decision and prior scope is still valid, continue without demanding blanket approval. If a material question remains, ask that focused question and continue independent work; wait for the answer or the user's explicit deferral before finalizing the affected decision.

Feedback that changes records, source coverage, dispositions or material qualifications requires refreshed semantic QA, an updated common review when needed, a new preview and display of the changes. Do not edit preview hashes or reuse a stale file to bypass the check.

```text
python scripts/memory_tool.py giga-export --memory common-memory.json --common-store common.sqlite --projection giga-projection.json --preview giga-preview.json --out client-memory-export.md --report giga-export-report.json --full-out selected-context-full.zip --assets selected-assets
```

The command must produce both the full selected portable package and the adapted Markdown delivery. The ZIP uses `common-memory.json`, its current common review and the supplied selected assets, never the constrained projection. `--full-out` overrides the default `<profile-stem>-full.zip`; omitting it does not omit the package. An asset declaration is mandatory: choose exactly one of `--assets selected-assets` or `--no-assets --asset-scope-note "Actual profile-only scope or unavailable originals"`. The no-assets route needs the real explanation; do not use it to bypass preservation of selected available files. Reuse known choices, with no new whole-scope interview. Keep the full ZIP outside the native upload folder and verify its returned path/hash. Limits and omissions in the native projection must not remove selected records or assets from the full package. Privacy-excluded records remain excluded, and their text is not automatically redacted from raw originals.

With no code execution, perform the same source/atom review and show the three groups manually before writing final Markdown; say program validation did not run. Create the full selected package with actual supported file tools or report it as unfinished. Neither route relaxes the ban on automatically importing temporary, clarification or reserve sections.

Finally use [completion.md](completion.md): give the full selected ZIP for the user's own storage, then two actual separate Markdown attachments for GigaAgent and the import kickoff text in the same reply. `NEXT_STEPS.md`, preview JSON, reports and backups remain with the user/sender; they are not extra files to upload into ordinary GigaAgent.
