"""Build self-contained Claude skills, a plugin and a manual web execution kit."""
import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portable_memory import __version__
ENTRY = 'from portable_memory.cli import main\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'


def zip_files(output, files):
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)


def build(output=None, *, web_kit_only=False):
    output = Path(output) if output else ROOT / "dist"
    output.mkdir(parents=True, exist_ok=True)
    core = {"portable_memory/" + file.name: file.read_bytes()
            for file in (ROOT / "portable_memory").glob("*.py")}
    plugin = {"plugin.json": (ROOT / "plugin.json").read_bytes()}
    artifacts = []
    for name in ("memory-export", "memory-import"):
        skill = ROOT / "skills" / name
        scripts = skill / "scripts"
        (scripts / "portable_memory").mkdir(parents=True, exist_ok=True)
        (scripts / "memory_tool.py").write_text(ENTRY, encoding="utf-8")
        for relative, content in core.items():
            (scripts / relative).write_bytes(content)
        files = {file.relative_to(skill).as_posix(): file.read_bytes()
                 for file in skill.rglob("*") if file.is_file() and
                 "__pycache__" not in file.parts and file.suffix != ".pyc"}
        archive = output / f"{name}.zip"
        if not web_kit_only:
            zip_files(archive, {name + "/" + path: content for path, content in files.items()})
        artifacts.append(archive.name)
        plugin.update({"skills/" + name + "/" + path: content for path, content in files.items()})
    if not web_kit_only:
        zip_files(output / "portable-memory-plugin.zip", plugin)
    kit = {"memory_tool.py": ENTRY.encode(), **core,
           "START_HERE.md": (ROOT / "docs" / "MANUAL_WEB.md").read_bytes()}
    kit.update({path: content for path, content in plugin.items() if path.startswith("skills/")})
    kit.update({file.relative_to(ROOT).as_posix(): file.read_bytes()
                for file in (ROOT / "examples").rglob("*") if file.is_file()})
    kit["JOURNEY.md"] = (ROOT / "docs" / "JOURNEY_V02.md").read_bytes()
    kit["WORKS_AND_SKILLS.md"] = (ROOT / "docs" / "WORKS_AND_SKILLS.md").read_bytes().replace(b"(../skills/", b"(skills/")
    for name in ("PROFILE_REVIEW.md", "CLOUD_DELIVERY.md", "PRODUCT_GOAL.md", "STORAGE_CHOICES.md", "RECEIVING_SETUP.md", "INSTRUCTIONS_AND_SKILLS.md", "AGENT_CONTEXT.md", "AGGREGATE_REVIEW.md", "IMPLEMENTATION_STATUS.md"):
        kit[name] = (ROOT / "docs" / name).read_bytes().replace(b"(../skills/", b"(skills/")
    report_version = "".join(__version__.split(".")[:2])
    for name in ("TESTING.md", "test-results.json", "common-profile-demo.json", f"skill-forward-test-v{report_version}.json"):
        if (ROOT / "docs" / name).is_file():
            kit[name] = (ROOT / "docs" / name).read_bytes()
    zip_files(output / "memory-web-kit.zip", kit)
    artifacts += ["portable-memory-plugin.zip", "memory-web-kit.zip"]
    result = {"version": __version__, "artifacts": artifacts,
              "note": "Built locally; live web installation and native-memory application need separate verification."}
    (output / "build-report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--web-kit-only", action="store_true", help="Refresh the web kit/reports without changing independently tested skill ZIPs")
    args = parser.parse_args()
    print(json.dumps(build(args.out, web_kit_only=args.web_kit_only), indent=2))
