"""A fully offline round trip using explicitly fictional review decisions."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portable_memory.model import confirm, review_template
from portable_memory.package import load_json, write_new, json_bytes
from scripts.synthetic_fixture_audit import export_synthetic_package
from portable_memory.importing import apply_import, plan_import


def demo(output):
    output = Path(output)
    if output.exists():
        raise ValueError("Choose a new demo output directory")
    profile = load_json(ROOT / "examples" / "profile.json")
    decisions = review_template(profile)
    # This is a synthetic fixture, never an automatic review policy for user data.
    for item in decisions["decisions"]:
        item["decision"] = {"project-1": "historical", "idea-1": "pending", "old-1": "exclude"}.get(item["id"], "active")
    reviewed = confirm(profile, decisions)
    write_new(output / "reviewed.json", json_bytes(reviewed))
    archive = output / "memory.zip"
    export_synthetic_package(reviewed, archive)
    plan = plan_import(archive)
    write_new(output / "plan.json", json_bytes(plan))
    receipt = apply_import(archive, plan, output / "recipient", accepted=True)
    again = plan_import(archive, load_json(output / "recipient" / "memory.json"))
    write_new(output / "repeat-plan.json", json_bytes(again))
    result = {"synthetic": True, "output": str(output), "first_import": receipt["records"],
              "repeat_import": again["items"], "native_memory": "not_applied"}
    write_new(output / "demo-report.json", json_bytes(result))
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    print(json.dumps(demo(parser.parse_args().out), ensure_ascii=False, indent=2))
