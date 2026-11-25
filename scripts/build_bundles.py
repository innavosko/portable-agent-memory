"""Build two standalone skills; combined developer packages are opt-in."""
import argparse
import io
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


def build(output=None, *, web_kit_only=False, include_extras=False):
    output = Path(output) if output else ROOT / "dist"
    output.mkdir(parents=True, exist_ok=True)
    core = {"portable_memory/" + file.name: file.read_bytes()
            for file in (ROOT / "portable_memory").glob("*.py")}
    plugin = {"plugin.json": (ROOT / "plugin.json").read_bytes()}
    artifacts = []
    # Build the receiving skill first so the exporter can supply its actual
    # installer as an output artifact, without requiring both roles installed.
    importer_bytes = None
    for name in ("memory-import", "memory-export"):
        skill = ROOT / "skills" / name
        if name == "memory-export":
            (skill / "assets").mkdir(exist_ok=True)
            (skill / "assets" / "memory-import.zip").write_bytes(importer_bytes)
        scripts = skill / "scripts"
        (scripts / "portable_memory").mkdir(parents=True, exist_ok=True)
        (scripts / "memory_tool.py").write_text(ENTRY, encoding="utf-8")
        for relative, content in core.items():
            (scripts / relative).write_bytes(content)
        files = {file.relative_to(skill).as_posix(): file.read_bytes()
                 for file in skill.rglob("*") if file.is_file() and
                 "__pycache__" not in file.parts and file.suffix != ".pyc"}
        archive = output / f"{name}.zip"
        if name == "memory-import":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as inner:
                for path, content in sorted(files.items()):
                    inner.writestr(name + "/" + path, content)
            importer_bytes = buffer.getvalue()
        if not web_kit_only:
            if name == "memory-import":
                archive.write_bytes(importer_bytes)
            else:
                zip_files(archive, {name + "/" + path: content for path, content in files.items()})
            artifacts.append(archive.name)
        plugin.update({"skills/" + name + "/" + path: content for path, content in files.items()})
    if not include_extras and not web_kit_only:
        result = {"version": __version__, "artifacts": artifacts,
                  "note": "Two independent skills: export for the sender, import for the receiver. Each contains its own runtime."}
        (output / "build-report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
        return result
    if not web_kit_only:
        zip_files(output / "portable-memory-plugin.zip", plugin)
        artifacts.append("portable-memory-plugin.zip")
    kit = {"memory_tool.py": ENTRY.encode(), **core,
           "START_HERE.md": (ROOT / "docs" / "MANUAL_WEB.md").read_bytes()}
    kit.update({path: content for path, content in plugin.items() if path.startswith("skills/")})
    kit.update({file.relative_to(ROOT).as_posix(): file.read_bytes()
                for file in (ROOT / "examples").rglob("*") if file.is_file()})
    kit["JOURNEY.md"] = (ROOT / "docs" / "JOURNEY_V02.md").read_bytes()
    kit["WORKS_AND_SKILLS.md"] = (ROOT / "docs" / "WORKS_AND_SKILLS.md").read_bytes().replace(b"(../skills/", b"(skills/")
    for name in ("PROFILE_REVIEW.md", "CLOUD_DELIVERY.md", "PRODUCT_GOAL.md", "STORAGE_CHOICES.md", "RECEIVING_SETUP.md", "INSTRUCTIONS_AND_SKILLS.md", "AGENT_CONTEXT.md", "AGGREGATE_REVIEW.md", "IMPLEMENTATION_STATUS.md"):
        kit[name] = (ROOT / "docs" / name).read_bytes().replace(b"(../skills/", b"(skills/")
    if (ROOT / "docs" / "UX_REVIEW_V08.md").is_file():
        kit["UX_REVIEW_V08.md"] = (ROOT / "docs" / "UX_REVIEW_V08.md").read_bytes().replace(b"(../skills/", b"(skills/")
    report_version = "".join(__version__.split(".")[:2])
    for name in ("TESTING.md", "test-results.json", "common-profile-demo.json", f"skill-forward-test-v{report_version}.json"):
        if (ROOT / "docs" / name).is_file():
            kit[name] = (ROOT / "docs" / name).read_bytes()
    zip_files(output / "memory-web-kit.zip", kit)
    artifacts.append("memory-web-kit.zip")
    result = {"version": __version__, "artifacts": artifacts,
              "note": "Built locally; live web installation and native-memory application need separate verification."}
    (output / "build-report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--web-kit-only", action="store_true", help="Refresh the web kit/reports without changing independently tested skill ZIPs")
    parser.add_argument("--include-extras", action="store_true", help="Developer-only: also build the combined plugin and manual web kit")
    args = parser.parse_args()
    print(json.dumps(build(args.out, web_kit_only=args.web_kit_only, include_extras=args.include_extras), indent=2))
