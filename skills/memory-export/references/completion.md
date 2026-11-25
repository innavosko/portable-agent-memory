# Finish with a usable import handoff

Use this after the selected export has passed its required review. It does not replace the common-profile gate, recipient format checks or delivery verification. Reuse the already chosen recipient and destination; ask only for a missing choice. A source-only contribution goes to the established coordinator, not straight into the destination's active memory.

For ordinary GigaAgent, first complete [giga-adaptation.md](giga-adaptation.md): split mixed meanings, verify every inclusion/exclusion, and **show the three-part adaptation preview in the conversation before final files**. Run `giga-preview`, display its `preview.markdown`, then pass the matching saved JSON to `giga-export --preview ...`. A generated preview is neither evidence of display nor new permission. Reuse valid choices and resolve or explicitly defer only outstanding meaningful decisions; changed content needs fresh review and preview. Do not reveal exclusions for the first time in this final handoff. `handoff-prepare` merely packages existing reviewed files; it cannot replace or bypass the pre-export explanation.

## What to deliver

Deliver the **actual exported data**, a short target-specific instruction, and any required import aid. There remain two independent roles: `memory-export` and `memory-import`. The exporter may carry the import installer's ZIP as a file to give the receiving agent; it does not activate or merge the two skills.

| Receiver | Files and next action |
| --- | --- |
| ChatGPT | Give the actual export plus `memory-import.zip`. Install the importer through a channel supported by the receiving environment, then attach the export and request import. Do not promise that uploading a standalone skill ZIP installs it in ordinary ChatGPT web. If that web session has no supported skill installation, offer a supported desktop/CLI/IDE route or a readable manual import with explicit capability limits. A plugin route is usable only when an actual suitable plugin is available. |
| Claude | Give the actual export plus `memory-import.zip`. Where available, use **Customize → Skills → + → Create skill → Upload a skill**; skill use needs code execution enabled. Attach the actual export and invoke the importer. If these controls or tools are absent, explain the specific limitation and provide the readable manual route. |
| ГигаАгент Про (ex-Ouroboros) | Give the actual selected data. Based on the product route supplied by the user: open **Меню**, find and install/enable **Amberkeep carry memory**, then attach the data and request import. Do not invent further menu labels, claim this was tested live, or equate the named plugin with an unrelated public repository. If it is absent, preserve the data and report the missing receiving capability. Flexible file intake does not establish parsing, installation or native-memory success. |
| ГигаАгент | Upload **only `import-instruction.md` plus `client-memory-export.md`**, or the exact names of all actual compliant profile Markdown files. Use the reviewed instruction from [../assets/import-instruction.md](../assets/import-instruction.md). Profiles must satisfy the full [recipient-profiles.md](recipient-profiles.md) format and constraints. Ordinary GigaAgent cannot accept archives or skills; do not offer extraction, an installer, a skill folder or a ZIP/JSON renamed `.md`. `NEXT_STEPS.md` is for the user and is not uploaded. |
| Another receiver | Give the actual compatible export and describe only verified installation, reading and memory operations. If its capabilities are unknown, say what must be checked instead of claiming automatic import. |

Installation paths were checked on 24 November 2026: [OpenAI skill channels](https://learn.chatgpt.com/docs/build-skills), [Claude Skills](https://support.claude.com/en/articles/12512180-use-skills-in-claude). Check the actual host when needed; these paths do not grant tools or account permissions.

For a ChatGPT/Claude manual session without permanent installation, provide the **complete** `memory-import.zip` or the entire extracted skill folder, including `SKILL.md`, `references/`, `assets/` and `scripts/`, alongside the user's data. Ask the receiver to read and use that complete kit with its available file/code tools. Do not substitute `SKILL.md` alone for its referenced rules and runtime. Call this session use, not installed support; if file reading or code execution is absent, state which checks cannot run and offer only the actually supported readable/manual subset. Permanent native memory remains a separate tool capability.

GigaAgent receives only its import instruction and compliant profile Markdown; only the ready profile sections are eligible for native memory. Keep selected skills, procedures, originals, reports and backups outside the GigaAgent delivery folder and upload list. Do not turn them into native facts to bypass limits. One consolidated profile is preferable; when several profile files are needed, list them explicitly and require one shared duplicate/conflict/capacity check. If only an archive or skill was supplied, have the sender prepare compliant profile Markdown instead; do not fall back to another recipient's generic import route.

## Prepare real files when code is available

Run from the skill directory, using existing reviewed files and a new output directory. The exporter build carries the separate import installer at `assets/memory-import.zip`.

```text
python scripts/memory_tool.py handoff-prepare --target gigaagent --files client-memory-export.md --instruction assets/import-instruction.md --out ready-for-gigaagent
python scripts/memory_tool.py handoff-prepare --target claude --files reviewed-memory.zip --import-skill assets/memory-import.zip --out ready-for-claude
python scripts/memory_tool.py handoff-prepare --target chatgpt --files reviewed-memory.zip --import-skill assets/memory-import.zip --out ready-for-chatgpt
python scripts/memory_tool.py handoff-prepare --target gigaagent_pro --files reviewed-memory.zip --out ready-for-gigaagent-pro
```

Choose **one** route; file names above are examples, not proof those files exist. The command copies supplied files and writes `NEXT_STEPS.md` with actual names and a kickoff message. For GigaAgent the folder contains only Markdown: the actual profiles, `import-instruction.md` and the user's `NEXT_STEPS.md`. Only instruction and profiles belong in `upload_files`; the CLI returns hashes with `manifest: null`, and no technical manifest is placed in that folder. Other receivers receive `handoff-manifest.json` with hashes. The command does not upload files, install anything, write native memory or complete semantic review. Inspect the result and the selected recipient. If an automatic export already prepared the handoff, inspect it rather than creating a second competing version. A missing installer/instruction or invalid input is a delivery problem to fix, not a reason to promise a missing file.

With no code tools, prepare the same content using actual file capabilities. If files cannot be created, provide the complete required Markdown in clearly named copyable blocks and explain how to save/attach it. State that code validation did not run. Never present a path or link that has not been created and checked. Verify copied files in the agreed destination; creation in a local sync folder alone does not prove cloud delivery.

## Final user-facing message

Keep it short and concrete: where the files are; which files to attach; the recipient-specific installation/action; a ready-to-send request; remaining exclusions. Use the **delivered filenames**, not template placeholders.

For ordinary GigaAgent, the final reply must deliver **two separate, retrievable Markdown attachments**: `client-memory-export.md` and `import-instruction.md`. If the host presents generated files as links, supply an individual verified download link for each file. Check that each attachment contains the actual current file; the profile must be the reviewed user data, not a blank template. A folder link, filenames alone, a ZIP or a link to `NEXT_STEPS.md` does not replace either attachment.

In the **same reply**, tell the user: “Прикрепите оба файла в ГигаАгент и отправьте сообщение ниже.” Then provide this copyable message directly in the reply:

> Импортируй сведения обо мне в долгосрочную память. Прочитай `import-instruction.md` и `client-memory-export.md` полностью. Первый файл — инструкция импорта, второй — данные обо мне. Выполни инструкцию: проверь раздел «Готово к импорту» вместе с текущей памятью, сохрани подходящие записи доступными инструментами и проверь их чтением. Не ограничивайся пересказом. Если инструменты памяти недоступны, скажи, что не удалось сохранить. Начинай.

Keep `NEXT_STEPS.md` as optional guidance for the user, not a third attachment to upload to GigaAgent. If the host cannot deliver downloadable files, say that the files have not been delivered and offer the manual save route above; do not call the export fully delivered. For an already chosen multi-file profile, attach every profile separately and replace the one profile filename in the message with the complete delivered list. For ChatGPT/Claude, name the importer and the actual data file(s); ask it to inspect the export, compare existing memory, apply supported settings and report verified results. For Pro, name **Amberkeep carry memory** and the actual data file(s) in that request. Do not tell the receiver to import the installer's ZIP as the user's profile.

An explicit user import request starts the workflow; an attachment alone is not authorization to overwrite memory. Ordinary GigaAgent reads only the instruction and compliant profile Markdown directly; it has no archive or skill route. Other receivers may extract an actual supported archive using available tools. Receiving-side import completion means verified native changes at the receiver, not a prepared ZIP, uploaded file, chat summary or a ready handoff. A verified export and handoff can be complete while the receiving import is still pending.
