"""The same entry point runs inside a web agent's Python environment or on desktop."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .archives import ingest_archive
from .errors import ValidationError
from .importing import apply_import, plan_import
from .model import confirm, render_review, review_template, validate_memory
from .package import export_package, json_bytes, load_json, read_package, write_new
from .transfers import apply_event, create_transfer, next_actions, validate_transfer
from .migration import (MigrationStore, collect_migration, create_migration,
                        migration_view, render_migration_view, validate_migration)
from .profile_audit import quality_template
from .profile_session import (apply_profile_event, create_profile, profile_result,
                              profile_view, refresh_profile, render_profile_view)
from .delivery import confirm_publication, prepare_delivery, verify_delivery
from .setup import apply_setup_event, render_setup_view, setup_view
from .storage import provider_catalog


def handoff_assets(target, *, import_skill=None, instruction=None):
    """Find the same assets in a source checkout or either standalone skill."""
    roots = [Path(__file__).resolve().parents[2] / "assets",
             Path(__file__).resolve().parents[1] / "skills" / "memory-export" / "assets"]
    def asset(name):
        return next((root / name for root in roots if (root / name).is_file()), roots[-1] / name)
    return {"import_skill": import_skill or (asset("memory-import.zip") if target in {"chatgpt", "claude"} else None),
            "instruction": instruction or (asset("import-instruction.md") if target == "gigaagent" else None)}


def export_handoff_options(args, target, *other_outputs):
    from .completion import RESERVED, preflight_handoff
    from .package import safe_name
    output = Path(args.out).resolve()
    safe_name(output.name)
    extension = ".md" if target == "gigaagent" else ".zip"
    if output.suffix.lower() != extension:
        raise ValidationError(f"Export output must have the {extension} extension")
    if output.name.casefold() in RESERVED:
        raise ValidationError("Choose a data filename distinct from the installer and import instructions")
    destination = Path(args.handoff_out).resolve() if args.handoff_out else output.with_name(output.stem + "-import")
    outputs = (output, *(Path(value).resolve() for value in other_outputs))
    for candidate in outputs:
        safe_name(candidate.name)
        if candidate.exists() or candidate.is_symlink():
            raise ValidationError(f"Output already exists: {candidate}")
        parent = candidate.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.is_dir():
            raise ValidationError("Export output has a parent that is not a directory")
        if candidate == destination or destination in candidate.parents or candidate in destination.parents:
            raise ValidationError("Export files and report must be outside the new import handoff directory")
        if any(other != candidate and (other in candidate.parents or candidate in other.parents) for other in outputs):
            raise ValidationError("Export data and report must be independent file paths")
    options = handoff_assets(target, import_skill=getattr(args, "import_skill", None))
    preflight_handoff(target, destination, **options)
    return destination, options


def parser():
    root = argparse.ArgumentParser(description="Portable memory: export, review and import without network calls")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("recipient-options", help="List recipient formats, not verified native capabilities")
    p = sub.add_parser("handoff-prepare", help="Copy actual exported files with recipient-specific next steps and required installer/instruction")
    p.add_argument("--target", required=True); p.add_argument("--files", nargs="+", required=True)
    p.add_argument("--out", required=True); p.add_argument("--import-skill"); p.add_argument("--instruction")
    p = sub.add_parser("journey-view", help="Show role, proactive offers and one unanswered export choice")
    p.add_argument("--store", required=True); p.add_argument("--actor")
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    for name in ("giga-template", "giga-preview", "giga-export"):
        p = sub.add_parser(name, help="Prepare reviewed Markdown for GigaAgent's bounded native memory")
        p.add_argument("--memory", required=True); p.add_argument("--common-store", required=True)
        p.add_argument("--out", required=True)
        if name in {"giga-preview", "giga-export"}:
            p.add_argument("--projection", required=True)
        if name == "giga-export":
            p.add_argument("--report", required=True)
            p.add_argument("--preview", help="Required current giga-preview JSON; show its Markdown to the user before final export")
            p.add_argument("--handoff-out", help="New folder for actual profile files, import instruction and next steps")
            p.add_argument("--full-out", help="Full selected portable package; defaults to <profile-stem>-full.zip outside the GigaAgent folder")
            p.add_argument("--assets", help="Explicitly selected originals, projects and skill files to preserve in the full package")
            p.add_argument("--no-assets", action="store_true", help="Declare that no original files are supplied; requires --asset-scope-note")
            p.add_argument("--asset-scope-note", help="Actual chosen file scope or unavailable originals; required with --no-assets")
    p = sub.add_parser("giga-quality-template", help="Prepare the unapproved target-projection review")
    p.add_argument("--projection", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("giga-plan", help="Plan GigaAgent native writes from Markdown and current memory, without applying them")
    p.add_argument("--document", required=True); p.add_argument("--snapshot", required=True)
    p.add_argument("--comparisons"); p.add_argument("--out", required=True)
    p = sub.add_parser("giga-comparison-template", help="Prepare an unapproved comparison against actual native memory")
    p.add_argument("--document", required=True); p.add_argument("--snapshot", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("giga-comparison-quality-template", help="Prepare the unapproved second semantic pass for native import")
    p.add_argument("--comparisons", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("giga-readback", help="Check supplied actual memory_read results; never call native tools")
    p.add_argument("--plan", required=True); p.add_argument("--observed", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("common-create", help="Create the single coordinator's common-profile store")
    p.add_argument("--store", required=True); p.add_argument("--coordinator", required=True)
    p.add_argument("--previous"); p.add_argument("--allow-partial", action="store_true"); p.add_argument("--scope-note")
    p.add_argument("--migration-store")
    p = sub.add_parser("common-source", help="Add supplied source records and invalidate the common model review")
    p.add_argument("--store", required=True); p.add_argument("--source", required=True); p.add_argument("--memory")
    p.add_argument("--revision", required=True, type=int); p.add_argument("--actor", required=True)
    for name in ("common-input", "common-template"):
        p = sub.add_parser(name, help="Give the coordinator the actual combined input or unapproved semantic checklist")
        p.add_argument("--store", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("common-quality-template", help="Prepare the unapproved second semantic pass")
    p.add_argument("review"); p.add_argument("--out", required=True)
    p = sub.add_parser("common-review", help="Record the actual whole-profile model assessment and its second pass")
    p.add_argument("--store", required=True)
    for name in ("assessment", "quality", "semantic", "semantic-quality"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--revision", required=True, type=int); p.add_argument("--actor", required=True)
    p = sub.add_parser("common-view", help="Show common review readiness and only the next relevant question")
    p.add_argument("--store", required=True)
    p = sub.add_parser("common-event", help="Record a real user decision or question state through the coordinator")
    p.add_argument("--store", required=True); p.add_argument("--event", required=True)
    p.add_argument("--revision", required=True, type=int); p.add_argument("--actor", required=True)
    p = sub.add_parser("common-sync", help="Invalidate the common review when the linked source map changes")
    p.add_argument("--store", required=True); p.add_argument("--revision", required=True, type=int); p.add_argument("--actor", required=True)
    p = sub.add_parser("common-result", help="Write a profile and report only after the mandatory current review")
    p.add_argument("--store", required=True); p.add_argument("--out", required=True); p.add_argument("--report", required=True)
    p = sub.add_parser("context-template", help="Prepare an unassessed Dots/Duet context checklist, without account access")
    p.add_argument("--platform", required=True, choices=["dots", "duet"])
    p.add_argument("--source", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("context-collect", help="Preserve provided agent context and explicitly report missing categories")
    p.add_argument("snapshot"); p.add_argument("--out", required=True); p.add_argument("--accept", action="store_true")
    p = sub.add_parser("skill-bind-context", help="Attach selected original project instructions to an exported skill copy")
    p.add_argument("memory"); p.add_argument("--assets", required=True); p.add_argument("--bindings", required=True)
    p.add_argument("--out", required=True); p.add_argument("--accept", action="store_true")
    p = sub.add_parser("skill-discover", help="List skill folders from one explicitly supplied root, without selecting or activating them")
    p.add_argument("folder"); p.add_argument("--source", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("skill-collect", help="Copy selected original skill bundles and report missing parts, without execution or installation")
    p.add_argument("catalog"); p.add_argument("--out", required=True); p.add_argument("--accept", action="store_true")
    p = sub.add_parser("ingest", help="Convert a provided ChatGPT/Claude JSON or ZIP to readable source files")
    p.add_argument("source"); p.add_argument("--out", required=True)
    p = sub.add_parser("validate", help="Validate structured records and their review state")
    p.add_argument("memory")
    p = sub.add_parser("review", help="Write a human-readable review and a pending decision template")
    p.add_argument("memory"); p.add_argument("--out", required=True)
    p = sub.add_parser("confirm", help="Apply explicit content-bound human review decisions")
    p.add_argument("memory"); p.add_argument("--decisions", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("export", help="Build a verified portable package")
    p.add_argument("memory"); p.add_argument("--out", required=True); p.add_argument("--assets")
    p.add_argument("--common-store", required=True)
    p.add_argument("--target", default="other", help="Receiving product; GigaAgent requires giga-export instead")
    p.add_argument("--handoff-out", help="New folder for actual export files and recipient-specific next steps")
    p.add_argument("--import-skill", help="Explicit receiving skill installer; otherwise use the bundled copy")
    p = sub.add_parser("inspect", help="Verify all package files and report coverage")
    p.add_argument("package")
    p = sub.add_parser("plan", help="Prepare a merge plan, without writing host memory")
    p.add_argument("package"); p.add_argument("--current"); p.add_argument("--out", required=True)
    p.add_argument("--current-source", choices=["provided_snapshot", "previous_portable_workspace"],
                   help="Origin of supplied current data; a previous portable workspace is not a fresh host-memory snapshot")
    p = sub.add_parser("apply", help="Apply an accepted plan to a NEW portable workspace only")
    p.add_argument("package"); p.add_argument("--plan", required=True); p.add_argument("--out", required=True)
    p.add_argument("--accept", action="store_true")
    p.add_argument("--setup-delivery", help="Actual recipient identity and selected setup scope JSON; creates setup.json and explicit next steps")
    p = sub.add_parser("transfer-create", help="Create one shared transfer card")
    p.add_argument("--config", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("transfer-event", help="Record an observed action against an expected revision")
    p.add_argument("card"); p.add_argument("--event", required=True); p.add_argument("--revision", type=int, required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("transfer-next", help="Show actions available to one agent")
    p.add_argument("card"); p.add_argument("--actor", required=True)
    p = sub.add_parser("migration-create", help="Create one local authority for a multi-source journey")
    p.add_argument("--config", required=True); p.add_argument("--store", required=True)
    p = sub.add_parser("migration-view", help="Show progress and at most one meaningful next question")
    p.add_argument("--store", required=True); p.add_argument("--actor")
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p = sub.add_parser("migration-event", help="Record a client choice against the latest state revision")
    p.add_argument("--store", required=True); p.add_argument("--event", required=True)
    p.add_argument("--revision", required=True, type=int)
    p = sub.add_parser("migration-collect", help="Collect independent supplied files concurrently, never wait for missing archives")
    p.add_argument("--store", required=True); p.add_argument("--out", required=True)
    p.add_argument("--actor"); p.add_argument("--workers", type=int, default=4)
    p = sub.add_parser("migration-export-state", help="Export a handoff snapshot, not a synchronized cloud store")
    p.add_argument("--store", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("migration-resume", help="Restore a snapshot as a new local authority after stopping its previous owner")
    p.add_argument("--state", required=True); p.add_argument("--store", required=True)
    p = sub.add_parser("profile-quality-template", help="Prepare an unapproved second-pass evidence review")
    p.add_argument("--memory", required=True); p.add_argument("--assessment", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("profile-create", help="Start profile audit, reusing unchanged user-confirmed facts")
    p.add_argument("--memory", required=True); p.add_argument("--assessment", required=True)
    p.add_argument("--quality"); p.add_argument("--previous"); p.add_argument("--out", required=True)
    p = sub.add_parser("profile-view", help="Show found facts, gaps, conflicts and one next clarification")
    p.add_argument("state"); p.add_argument("--format", choices=["json", "markdown"], default="json")
    p = sub.add_parser("profile-event", help="Record a shown question, actual user decision or coverage update")
    p.add_argument("state"); p.add_argument("--event", required=True); p.add_argument("--revision", type=int, required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("profile-refresh", help="Add new evidence without repeating the unchanged interview")
    p.add_argument("state"); p.add_argument("--memory", required=True); p.add_argument("--assessment", required=True)
    p.add_argument("--quality"); p.add_argument("--revision", type=int, required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("profile-result", help="Prepare profile records and an honest quality report, without applying host memory")
    p.add_argument("state"); p.add_argument("--out", required=True); p.add_argument("--report", required=True)
    sub.add_parser("storage-options", help="List storage choices, without asserting connector availability")
    p = sub.add_parser("delivery-prepare", help="Prepare a verified version folder and a separate sender ticket, without cloud upload")
    p.add_argument("package"); p.add_argument("--config", required=True); p.add_argument("--root", required=True)
    p.add_argument("--ticket-out", required=True)
    p = sub.add_parser("delivery-check", help="Check the copied folder against the trusted ticket and intended recipient")
    p.add_argument("folder"); p.add_argument("--ticket", required=True); p.add_argument("--recipient", required=True)
    p = sub.add_parser("delivery-published", help="Verify actual cloud readback files and record the host's publication observation")
    p.add_argument("folder"); p.add_argument("--ticket", required=True); p.add_argument("--location", required=True)
    p.add_argument("--observation", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("delivery-receive", help="Plan a folder import; with acceptance, write a portable workspace and receiving setup state")
    p.add_argument("folder"); p.add_argument("--ticket", required=True); p.add_argument("--recipient", required=True)
    p.add_argument("--store", required=True); p.add_argument("--root", required=True)
    p.add_argument("--current"); p.add_argument("--plan"); p.add_argument("--location")
    p.add_argument("--accept", action="store_true")
    p = sub.add_parser("delivery-save-setup", help="Save the latest receiving setup snapshot in its local delivery registry")
    p.add_argument("--store", required=True); p.add_argument("--state", required=True)
    p = sub.add_parser("delivery-receipt", help="Read the recorded result of one delivery version for one recipient")
    p.add_argument("--store", required=True); p.add_argument("--transfer-id", required=True)
    p.add_argument("--version", required=True, type=int); p.add_argument("--recipient", required=True)
    p = sub.add_parser("setup-view", help="Show portable receipt and per-step receiving configuration results separately")
    p.add_argument("state"); p.add_argument("--format", choices=["json", "markdown"], default="json")
    p = sub.add_parser("setup-event", help="Record an actual host capability or setup result, without performing native operations")
    p.add_argument("state"); p.add_argument("--event", required=True); p.add_argument("--revision", type=int, required=True)
    p.add_argument("--out", required=True)
    return root


def run(args):
    command = args.command
    if command == "handoff-prepare":
        from .completion import prepare_handoff
        return prepare_handoff(args.target, args.files, args.out,
                               **handoff_assets(args.target, import_skill=args.import_skill, instruction=args.instruction))
    if command == "journey-view":
        from .guidance import journey_view, render_journey_view
        view = journey_view(MigrationStore(args.store).load(), args.actor)
        return render_journey_view(view) if args.format == "markdown" else view
    if command == "recipient-options" or command.startswith("giga-"):
        from .recipients import (recipient_catalog, giga_projection_template, giga_projection_quality_template,
                                 build_giga_export, parse_giga_export, plan_giga_import,
                                 giga_comparison_template, giga_comparison_quality_template, verify_giga_readback,
                                 build_giga_preview, validate_giga_preview)
        if command == "recipient-options":
            return recipient_catalog()
        if command == "giga-quality-template":
            write_new(args.out, json_bytes(giga_projection_quality_template(load_json(args.projection))))
            return {"output": args.out, "semantic_review_required": True}
        if command == "giga-comparison-quality-template":
            write_new(args.out, json_bytes(giga_comparison_quality_template(load_json(args.comparisons))))
            return {"output": args.out, "semantic_review_required": True}
        if command == "giga-readback":
            result = verify_giga_readback(load_json(args.plan), load_json(args.observed))
            write_new(args.out, json_bytes(result))
            return result
        if command == "giga-comparison-template":
            document = parse_giga_export(Path(args.document).read_text(encoding="utf-8-sig"))
            result = giga_comparison_template(document, load_json(args.snapshot))
            write_new(args.out, json_bytes(result))
            return {"output": args.out, "semantic_review_required": True, "native_memory_applied": False}
        if command == "giga-plan":
            document = parse_giga_export(Path(args.document).read_text(encoding="utf-8-sig"))
            plan = plan_giga_import(document, load_json(args.snapshot),
                                    load_json(args.comparisons) if args.comparisons else None)
            write_new(args.out, json_bytes(plan))
            return {"output": args.out, "native_memory_applied": False}
        from .common_profile import CommonStore
        from .model import digest
        memory = load_json(args.memory)
        checked = CommonStore(args.common_store).export_review()
        if digest(memory) != digest(checked["memory"]):
            raise ValidationError("GigaAgent projection requires the current common-review result")
        if command == "giga-template":
            result = giga_projection_template(memory, checked["review"])
            write_new(args.out, json_bytes(result))
            return {"output": args.out, "projection_review_required": True}
        if command == "giga-preview":
            preview = build_giga_preview(memory, checked["review"], load_json(args.projection))
            write_new(args.out, json_bytes(preview))
            return {"output": args.out, "preview": preview, "native_memory_applied": False}
        from .dual_export import export_giga_bundle
        return export_giga_bundle(memory, checked["review"], load_json(args.projection),
                                  load_json(args.preview) if args.preview else None,
                                  output=args.out, report=args.report, full_output=args.full_out,
                                  handoff_output=args.handoff_out, assets=args.assets,
                                  no_assets=args.no_assets, asset_scope_note=args.asset_scope_note,
                                  instruction=handoff_assets("gigaagent")["instruction"])
    if command.startswith("common-"):
        from .common_profile import (CommonStore, add_source, common_event, common_input, common_template,
                                     common_view, create_common, review_common)
        if command == "common-quality-template":
            from .semantic_review import semantic_quality_template
            write_new(args.out, json_bytes(semantic_quality_template(load_json(args.review))))
            return {"output": args.out, "review_required": True}
        if command == "common-create":
            state = create_common(args.coordinator, load_json(args.previous) if args.previous else None,
                                  args.allow_partial, args.scope_note)
            CommonStore.create(args.store, state, args.migration_store)
            return common_view(state)
        store = CommonStore(args.store)
        if command == "common-sync":
            return common_view(store.sync(args.revision, args.actor))
        if command == "common-source":
            state = store.update(lambda current: add_source(current, load_json(args.source), load_json(args.memory) if args.memory else None,
                expected_revision=args.revision, actor=args.actor))
            return common_view(state)
        state = store.load()
        if command == "common-view":
            result = common_view(state)
            try:
                store.ensure_current(state)
            except ValidationError as exc:
                result.update(status="source_map_changed", export_allowed=False, question=None, next_action=str(exc))
            return result
        store.ensure_current(state)
        if command in {"common-input", "common-template"}:
            result = common_input(state) if command == "common-input" else common_template(state)
            write_new(args.out, json_bytes(result))
            return {"output": args.out, "coordinator": state["coordinator"], "revision": state["revision"]}
        if command == "common-review":
            def apply_review(current):
                store.ensure_current(current)
                return review_common(current, load_json(args.assessment), load_json(args.quality), load_json(args.semantic),
                    load_json(args.semantic_quality), expected_revision=args.revision, actor=args.actor)
            return common_view(store.update(apply_review))
        if command == "common-event":
            return common_view(store.update(lambda current: common_event(current, load_json(args.event),
                expected_revision=args.revision, actor=args.actor)))
        if command == "common-result":
            if Path(args.out).exists() or Path(args.report).exists():
                raise ValidationError("Choose new common result and report paths")
            result = store.export_review()
            write_new(args.out, json_bytes(result["memory"]))
            write_new(args.report, json_bytes({"review": result["review"], "profile": result["report"]}))
            return {"memory": args.out, "report": args.report, "readiness": result["review"]["readiness"]}
        raise ValidationError("Unknown common profile operation")
    if command == "context-template":
        from .agent_context import context_template
        snapshot = context_template(args.platform, load_json(args.source))
        write_new(args.out, json_bytes(snapshot))
        return {"snapshot": args.out, "assessment_required": True, "account_access": "not_performed"}
    if command == "context-collect":
        from .agent_context import collect_context
        return collect_context(load_json(args.snapshot), args.out, accepted=args.accept)
    if command == "skill-bind-context":
        from .skill_context import bind_skill_context
        return bind_skill_context(load_json(args.memory), args.assets, load_json(args.bindings), args.out, accepted=args.accept)
    if command == "skill-discover":
        from .skill_inventory import discover_skills
        if Path(args.out).exists():
            raise ValidationError("Catalog output already exists; choose a new file")
        catalog = discover_skills(args.folder, load_json(args.source))
        write_new(args.out, json_bytes(catalog))
        return {"catalog": args.out, "skills_found": len(catalog["items"]),
                "selection_required": True, "account_inventory": "not_automatically_discovered"}
    if command == "skill-collect":
        from .skill_inventory import collect_skills, validate_catalog
        catalog = load_json(args.catalog)
        validate_catalog(catalog)
        # A provided catalog can reference files next to itself in a web upload.
        for item in catalog["items"]:
            locator = item["locator"]
            if locator["kind"] in {"folder", "zip"}:
                path = Path(locator["path"])
                if not path.is_absolute():
                    locator["path"] = str(Path(args.catalog).absolute().parent / path)
        return collect_skills(catalog, args.out, accepted=args.accept)
    if command == "storage-options":
        return provider_catalog()
    if command == "delivery-prepare":
        ticket_path = Path(args.ticket_out)
        if ticket_path.exists() or ticket_path.is_symlink():
            raise ValidationError("Ticket output already exists; choose a new file before preparing delivery")
        config = load_json(args.config)
        # A ticket is an independently supplied expectation, not a fourth file
        # inside the three-file copied envelope. Reject this before preparation
        # when its version path can be determined from an explicit transfer id.
        if isinstance(config, dict) and isinstance(config.get("transfer_id"), str) and type(config.get("version")) is int:
            folder = Path(args.root) / "Перенос памяти" / config["transfer_id"] / "outbox" / f"package-v{config['version']:04d}"
            if ticket_path.resolve().is_relative_to(folder.resolve()):
                raise ValidationError("Save the sender ticket separately from the copied version folder")
        result = prepare_delivery(args.package, args.root, config)
        if ticket_path.resolve().is_relative_to(Path(result["folder"]).resolve()):
            raise ValidationError("Save the sender ticket separately from the copied version folder")
        write_new(ticket_path, json_bytes(result["ticket"]))
        return {**result, "ticket_output": str(ticket_path)}
    if command == "delivery-check":
        check = verify_delivery(args.folder, load_json(args.ticket), load_json(args.recipient))
        return check["report"]
    if command == "delivery-published":
        receipt = confirm_publication(args.folder, load_json(args.ticket), load_json(args.location), args.observation)
        write_new(args.out, json_bytes(receipt))
        return {**receipt, "output": args.out}
    if command == "delivery-receive":
        from .delivery_store import receive_delivery
        return receive_delivery(args.folder, load_json(args.ticket), load_json(args.recipient), args.store, args.root,
                                current=load_json(args.current) if args.current else None,
                                plan=load_json(args.plan) if args.plan else None,
                                location=load_json(args.location) if args.location else None, accepted=args.accept)
    if command == "delivery-save-setup":
        from .delivery_store import save_setup
        return save_setup(args.store, load_json(args.state))
    if command == "delivery-receipt":
        from .delivery_store import get_receipt
        return get_receipt(args.store, args.transfer_id, args.version, load_json(args.recipient))
    if command == "setup-view":
        view = setup_view(load_json(args.state))
        return render_setup_view(view) if args.format == "markdown" else view
    if command == "setup-event":
        state = apply_setup_event(load_json(args.state), load_json(args.event), expected_revision=args.revision)
        write_new(args.out, json_bytes(state))
        return setup_view(state)
    if command == "profile-quality-template":
        value = quality_template(load_json(args.memory), load_json(args.assessment))
        write_new(args.out, json_bytes(value))
        return {"output": args.out, "note": "A template is not a completed quality check."}
    if command == "profile-create":
        state = create_profile(load_json(args.memory), load_json(args.assessment),
                               load_json(args.quality) if args.quality else None,
                               load_json(args.previous) if args.previous else None)
        write_new(args.out, json_bytes(state))
        return profile_view(state)
    if command == "profile-view":
        view = profile_view(load_json(args.state))
        return render_profile_view(view) if args.format == "markdown" else view
    if command == "profile-event":
        state = apply_profile_event(load_json(args.state), load_json(args.event), expected_revision=args.revision)
        write_new(args.out, json_bytes(state))
        return profile_view(state)
    if command == "profile-refresh":
        state = refresh_profile(load_json(args.state), load_json(args.memory), load_json(args.assessment),
                                load_json(args.quality) if args.quality else None, expected_revision=args.revision)
        write_new(args.out, json_bytes(state))
        return profile_view(state)
    if command == "profile-result":
        result = profile_result(load_json(args.state))
        require_paths = (Path(args.out), Path(args.report))
        if require_paths[0].resolve() == require_paths[1].resolve() or any(path.exists() for path in require_paths):
            raise ValidationError("Choose two distinct new output files for memory and report")
        write_new(args.out, json_bytes(result["memory"]))
        write_new(args.report, json_bytes(result["report"]))
        return {"memory": args.out, "report": args.report, "native_memory": "not_applied",
                "unreviewed_records": result["report"]["unreviewed_records"]}
    if command == "migration-create":
        config = load_json(args.config)
        # Relative input paths in a config are relative to that file, not the caller's cwd.
        for observation in config.get("sources", []):
            item = observation.get("input")
            if item and isinstance(item.get("path"), str):
                path = Path(item["path"]).expanduser()
                if not path.is_absolute():
                    path = Path(args.config).resolve().parent / path
                item["path"] = str(path.resolve())
        store = MigrationStore.create(args.store, create_migration(config))
        return migration_view(store.load())
    if command == "migration-resume":
        state = load_json(args.state); validate_migration(state)
        store = MigrationStore.create(args.store, state)
        return migration_view(store.load())
    if command == "migration-view":
        view = migration_view(MigrationStore(args.store).load(), args.actor)
        return render_migration_view(view) if args.format == "markdown" else view
    if command == "migration-event":
        state = MigrationStore(args.store).event(load_json(args.event), expected_revision=args.revision)
        return migration_view(state)
    if command == "migration-collect":
        return collect_migration(MigrationStore(args.store), args.out, actor=args.actor, max_workers=args.workers)
    if command == "migration-export-state":
        state = MigrationStore(args.store).load()
        write_new(args.out, json_bytes(state))
        return {"output": args.out, "revision": state["revision"],
                "note": "Stop the previous authority before resuming elsewhere; transfer collected files separately."}
    if command == "ingest":
        return ingest_archive(args.source, args.out)
    if command == "validate":
        data = load_json(args.memory); validate_memory(data)
        return {"valid": True, "records": len(data["records"])}
    if command == "review":
        data = load_json(args.memory)
        report, template = render_review(data), review_template(data)
        decision_path = str(args.out) + ".decisions.json"
        if Path(args.out).exists() or Path(decision_path).exists():
            raise ValidationError("Review output already exists; choose new filenames")
        write_new(args.out, report); write_new(decision_path, json_bytes(template))
        return {"review": args.out, "decisions": decision_path}
    if command == "confirm":
        data = confirm(load_json(args.memory), load_json(args.decisions))
        write_new(args.out, json_bytes(data))
        return {"output": args.out, "records": len(data["records"])}
    if command == "export":
        from .common_profile import CommonStore
        from .completion import prepare_handoff
        if args.target == "gigaagent":
            raise ValidationError("GigaAgent requires reviewed Markdown: use giga-template and giga-export, not portable ZIP")
        destination, options = export_handoff_options(args, args.target)
        result = CommonStore(args.common_store).export_package(load_json(args.memory), args.out, assets=args.assets)
        result["handoff"] = prepare_handoff(args.target, [args.out], destination, **options)
        return result
    if command == "inspect":
        data = read_package(args.package)
        return {"valid": True, "sha256": data["sha256"], "manifest": data["manifest"]}
    if command == "plan":
        result = plan_import(args.package, load_json(args.current) if args.current else None,
                             current_source=args.current_source)
        write_new(args.out, json_bytes(result))
        return {"plan": args.out, "items": result["items"],
                "current_source": result["current_source"],
                "recipient_memory_required": result["recipient_memory_required"],
                "limitations": result["limitations"]}
    if command == "apply":
        return apply_import(args.package, load_json(args.plan), args.out, accepted=args.accept,
                            setup_delivery=load_json(args.setup_delivery) if args.setup_delivery else None)
    if command == "transfer-create":
        result = create_transfer(**load_json(args.config))
    elif command == "transfer-event":
        result = apply_event(load_json(args.card), load_json(args.event), expected_revision=args.revision)
    elif command == "transfer-next":
        card = load_json(args.card); validate_transfer(card)
        return {"actions": next_actions(card, args.actor)}
    else:
        raise ValidationError("Unknown command")
    write_new(args.out, json_bytes(result))
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValidationError, ValueError, OSError, TypeError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
