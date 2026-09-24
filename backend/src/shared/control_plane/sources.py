"""Import existing skill files into the Vicoa registry.

Reads sources. Does not modify them and does not copy a second library.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .store import ControlPlane, ControlPlaneError

ACTIVE = {
    "plan": ["planning", "general"],
    "github-pr-workflow": ["git", "general"],
    "github-issues": ["git", "general"],
    "github-code-review": ["git", "review", "general"],
    "code-review-expert": ["review", "general"],
    "requesting-code-review": ["review", "security", "general"],
    "test-driven-development": ["testing", "verification", "general"],
    "systematic-debugging": ["testing", "general"],
    "procoder": ["general"],
    "grounded-citations": ["research", "general"],
    "wordpress-rest-ops": ["wordpress", "php", "tillpress", "security"],
    "php-phpunit-testing": ["php", "testing", "verification"],
    "woo-plugin-suite": ["wordpress", "php"],
    "pre-launch-audit": ["security", "review", "general"],
    "github-reuse-first": ["git", "general"],
    "vicoa-session-handoff": ["handoff", "general"],
    "vicoa-deterministic-verification": ["verification", "testing", "general"],
}
SKIP_NAMES = {
    "dogepick",
    "rollercoin",
    "clashfarm-live-attack",
    "adb-vision-bots",
    "youtube-content",
    "batchideo-codebase",
    "ironveil-build",
    "nightsmith-build",
    "computer-use",
    "hermes-agent",
}
SAFETY_MARKERS = ("do not", "never", "refuse", "must not")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, text[end + 4 :]


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return fallback


def classify_path(path: Path, kind: str) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = _frontmatter(text)
    name = meta.get("name") or path.parent.name if path.name == "SKILL.md" else path.stem
    name = name.strip() or path.stem
    description = meta.get("description") or _title(text, name)
    domains = ["general"]
    decision = "skip"
    detail = "not a reusable coding skill"
    compatibility = ["any"]
    scope = "global"
    scope_ref = ""
    lower = f"{path} {name} {description}".lower()
    if path.name.lower() in {"readme.md", "contributing.md", "license", "license.md"}:
        decision, detail = "skip", "repository documentation"
    elif name in SKIP_NAMES or any(token in lower for token in ("roblox", "youtube", "batchideo", "dogepick", "rollercoin")):
        decision, detail = "skip", "domain-specific or blocked source"
        if "youtube" in lower:
            domains = ["youtube"]
        elif "batchideo" in lower:
            domains = ["batchideo"]
        elif "roblox" in lower:
            domains = ["roblox"]
    elif name.startswith("hermes-") or "hermes-only" in lower:
        decision, detail = "import_inactive", "provider-specific; not injected into Antigravity"
        compatibility = ["hermes"]
        domains = ["general"]
    elif name in ACTIVE:
        decision, detail = "import_active", "curated initial set"
        domains = ACTIVE[name]
    elif kind == "hermes" and any(part in path.parts for part in ("github", "software-development", "research", "productivity")):
        decision, detail = "import_inactive", "reusable but not in the initial active set"
    elif kind == "workspace" and "tillpress" in lower:
        decision, detail = "import_inactive", "project reference, not always-on context"
        scope, scope_ref, domains = "project", "tillpress", ["tillpress"]
    elif kind == "workspace" and path.suffix == ".md" and path.name[:2].isdigit():
        decision, detail = "import_inactive", "planning prompt kept on demand"
        domains = ["planning", "general"]
        name = path.stem
    return {
        "path": str(path),
        "name": name,
        "description": description[:500],
        "body": body.strip() or text.strip(),
        "hash": file_hash(path),
        "kind": kind,
        "decision": decision,
        "detail": detail,
        "domains": domains,
        "compatibility": compatibility,
        "scope": scope,
        "scope_ref": scope_ref,
    }


def inventory(roots: list[tuple[Path, str]]) -> list[dict]:
    rows: list[dict] = []
    for root, kind in roots:
        if not root.exists():
            continue
        files = list(root.rglob("SKILL.md"))
        if kind == "workspace":
            files.extend(path for path in root.glob("*.md"))
        for path in sorted(files):
            if "node_modules" in path.parts or ".git" in path.parts:
                continue
            rows.append(classify_path(path, kind))
    return rows


def _native(plane: ControlPlane) -> None:
    specs = [
        (
            "vicoa-session-handoff",
            "Session handoff",
            "Continue from the structured packet. Do not repeat a passed phase.",
            "Read the handoff packet. Continue the same task and worktree. Do not rerun a completed phase. Do not replay an acknowledged message.",
        ),
        (
            "vicoa-deterministic-verification",
            "Deterministic verification",
            "Prove the result with a check, not a claim.",
            "Run the named file or command check. A passing claim without the check is not verification.",
        ),
    ]
    for key, name, description, body in specs:
        row = plane.knowledge.register_skill(
            skill_key=key,
            name=name,
            description=description,
            body=body,
            scope="global",
            risk="low",
            source="manual",
            domains=ACTIVE[key],
            source_kind="native",
            compatible_agents=["any"],
        )
        plane.knowledge.record_canary(row["id"], passed=True, evidence={"summary": "native skill text has no secret and states the check"})
        plane.knowledge.activate_skill(row["id"])
        plane.knowledge.record_import(source_path="native", content_hash=row["content_hash"], skill_key=key, decision="import_active", detail="authored in Vicoa")


def import_rows(plane: ControlPlane, rows: list[dict]) -> dict:
    seen: dict[str, str] = {}
    counts = {"imported": 0, "active": 0, "duplicate": 0, "conflict": 0, "skip": 0}
    for row in rows:
        if row["decision"] == "skip":
            plane.knowledge.record_import(source_path=row["path"], content_hash=row["hash"], skill_key=row["name"], decision="skip", detail=row["detail"])
            counts["skip"] += 1
            continue
        prior = seen.get(row["hash"])
        if prior:
            plane.knowledge.record_import(source_path=row["path"], content_hash=row["hash"], skill_key=prior, decision="duplicate", detail=f"same hash as {prior}")
            counts["duplicate"] += 1
            continue
        key = row["name"]
        existing = [item for item in plane.knowledge.list_skills() if item["skill_key"] == key]
        if existing and existing[-1]["content_hash"] != hashlib.sha256(row["body"].encode()).hexdigest():
            old = existing[-1]["description"].lower()
            new = row["body"].lower()
            if any(marker in old and marker in new for marker in SAFETY_MARKERS):
                plane.knowledge.record_import(source_path=row["path"], content_hash=row["hash"], skill_key=key, decision="conflict", detail="safety text differs; kept both, did not replace")
                counts["conflict"] += 1
                key = f"{key}-alt"
            else:
                plane.knowledge.record_import(source_path=row["path"], content_hash=row["hash"], skill_key=key, decision="duplicate", detail="same skill key")
                counts["duplicate"] += 1
                continue
        try:
            saved = plane.knowledge.register_skill(
                skill_key=key,
                name=row["name"],
                description=row["description"],
                body=row["body"],
                scope=row["scope"],
                scope_ref=row["scope_ref"],
                risk="low",
                source="imported",
                domains=row["domains"],
                source_path=row["path"],
                source_kind=row["kind"],
                compatible_agents=row["compatibility"],
            )
        except ControlPlaneError as exc:
            plane.knowledge.record_import(source_path=row["path"], content_hash=row["hash"], skill_key=key, decision="skip", detail=exc.code)
            counts["skip"] += 1
            continue
        seen[row["hash"]] = key
        activate = row["decision"] == "import_active" and not saved.get("secrets_removed")
        if activate:
            plane.knowledge.record_canary(saved["id"], passed=True, evidence={"summary": "parsed source, redacted, and classified before activation"})
            plane.knowledge.activate_skill(saved["id"])
            counts["active"] += 1
        plane.knowledge.record_import(source_path=row["path"], content_hash=row["hash"], skill_key=key, decision=row["decision"], detail=row["detail"])
        counts["imported"] += 1
    return counts


def import_sources(plane: ControlPlane, roots: list[tuple[Path, str]]) -> dict:
    rows = inventory(roots)
    _native(plane)
    counts = import_rows(plane, rows)
    counts["discovered"] = len(rows)
    return {"rows": rows, "counts": counts}


def render_inventory(rows: list[dict]) -> str:
    lines = ["| Source | Name | Hash | Scope | Decision |", "| --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(f"| {row['kind']} | {row['name']} | {row['hash'][:12]} | {row['scope']} | {row['decision']} |")
    return "\n".join(lines) + "\n"
