"""Offline folder handoff with fictional data; no cloud or native agent calls."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portable_memory.delivery import prepare_delivery, verify_delivery
from portable_memory.delivery_store import receive_delivery, save_setup
from portable_memory.model import confirm, empty_memory, record_digest
from portable_memory.package import json_bytes, write_new
from scripts.synthetic_fixture_audit import export_synthetic_package
from portable_memory.setup import apply_setup_event, render_setup_view


def demo(output):
    output = Path(output).absolute()
    if output.exists():
        raise ValueError("Choose a new demo folder")
    output.mkdir(parents=True)
    memory = empty_memory()
    memory["metadata"] = {"synthetic": True}
    specs = [("tone", "preference", "Стиль ответа", "Коротко и по делу"),
             ("mail", "integration", "Рабочая почта", {"address": "nina@example.org", "rule": "Сначала показывать черновик"})]
    for rid, kind, title, value in specs:
        memory["records"].append({"id": rid, "kind": kind, "title": title, "value": value,
            "scope": "global", "sources": [{"id": "demo", "locator": "Вымышленный ответ пользователя", "observed_at": None}],
            "provenance": "user_statement", "currency": "unknown", "review": {"decision": "pending"}, "links": []})
    memory = confirm(memory, {"decisions": [{"id": r["id"], "content_sha256": record_digest(r), "decision": "active"} for r in memory["records"]]})
    package = output / "memory.zip"
    export_synthetic_package(memory, package)
    recipient = {"agent_id": "fictional-receiving-agent", "workspace_id": "fictional-personal-space"}
    config = {"transfer_id": "demo-transfer", "version": 1,
              "sender": {"agent_id": "fictional-sending-agent", "workspace_id": "fictional-source"},
              "recipient": recipient, "setup_scope": ["profile", "integrations"],
              "route": {"sender": {"provider": "google_drive", "access": "manual"},
                        "recipient": {"provider": "mail_cloud", "access": "manual"}, "copy_mode": "user_copy"}}
    prepared = prepare_delivery(package, output / "fictional-sender-files", config)
    write_new(output / "ticket.json", json_bytes(prepared["ticket"]))
    copied = output / "fictional-recipient-files" / Path(prepared["recipient_relative_folder"])
    shutil.copytree(prepared["folder"], copied)
    checked = verify_delivery(copied, prepared["ticket"], recipient)
    args = (copied, prepared["ticket"], recipient, output / "local-ledger.sqlite", output / "local-results")
    # Explicit fictional native snapshot; a missing snapshot is not empty.
    current = empty_memory()
    preview = receive_delivery(*args, current=current)
    received = receive_delivery(*args, current=current, plan=preview["plan"], accepted=True)
    state = received["setup"]
    events = [
        {"id": "demo-capability", "actor": "agent", "type": "capability", "data": {
            "name": "native_memory", "available": True, "evidence": "SYNTHETIC host capability, no live account checked"}},
        {"id": "demo-result", "actor": "agent", "type": "step_result", "data": {
            "step_id": "profile", "status": "verified", "evidence": "SYNTHETIC native write, no live account changed",
            "read_back": "SYNTHETIC read-back matches the demo preference"}},
    ]
    for event in events:
        state = apply_setup_event(state, event, expected_revision=state["revision"])
    saved = save_setup(args[3], state)
    repeated = receive_delivery(*args, accepted=True)
    assert repeated["reused"] and repeated["setup"] == saved["setup"]
    assert repeated["setup_view"]["status"] == "needs_setup"
    report = {"scenario": "standard-folder-handoff", "synthetic": True, "network_calls": 0,
              "sender_choice": "google_drive", "recipient_choice": "mail_cloud", "cloud_publication": "not_tested",
              "copied_files_verified": checked["report"]["files_checked"], "preview_performed_no_writes": not preview["writes_performed"],
              "records_imported": len(received["setup"]["memory"]["records"]), "repeat_reused": repeated["reused"],
              "setup_state_preserved": repeated["setup"] == saved["setup"],
              "setup_status": repeated["setup_view"]["status"], "native_changes": "none; simulated attestations only"}
    write_new(output / "demo-report.json", json_bytes(report))
    write_new(output / "setup-progress.md", "Все сведения и результаты внешних действий ниже — вымышленные.\n\n" + render_setup_view(repeated["setup_view"]))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    print(json.dumps(demo(parser.parse_args().out), ensure_ascii=False, indent=2))
