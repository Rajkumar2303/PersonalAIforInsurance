"""Read-only privacy scan for the submission (Step 10).

Scans tracked + untracked text files in the repo (excluding .git, venv,
node_modules, reports, memories, vault, browser profiles) for sensitive markers:

- Ontario licence number, VIN, date-of-birth, postal code, phone, email
- AWS keys, generic API keys/secrets/tokens, private keys, .env presence

Output: PATH + CATEGORY + ACTION only. Values are NEVER printed. Nothing is
deleted or modified. The scan is advisory; the participant decides what to do.

Usage:
    python demos/privacy_scan.py [repo_root]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "reports", "memories",
    ".vscode", ".idea", "htmlcov", "playwright-report", "test-results",
    "blob-report", "playwright/.cache", "frontend/dist", "frontend/.vite",
}

EXCLUDE_FILE_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                         ".woff", ".woff2", ".ttf", ".otf", ".eot", ".map",
                         ".zip", ".gz", ".lock", ".db", ".sqlite", ".sqlite3",
                         ".exe", ".dll", ".pem", ".key", ".p12", ".pdb"}

PATTERNS = [
    ("ontario_licence", re.compile(r"\b[A-Z]\d{4}-\d{5}-\d{5}\b")),
    ("vin", re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")),
    ("date_of_birth", re.compile(r"(?i)(dob|date\s*of\s*birth|birthdate)\s*[:=]\s*\d{4}-\d{2}-\d{2}")),
    ("postal_code", re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b")),
    ("phone", re.compile(r"(?<!\d)(?:\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4})(?!\d)")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic_secret", re.compile(
        r"(?i)\b(api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

# Categories we do not want to flood the report with (common in code/tests).
EMAIL_ALLOWLIST = ("@example.com", "@test", "@localhost", "your@email")


def _ignored(path: Path) -> bool:
    parts = path.parts
    if any(ex in parts for ex in EXCLUDE_DIRS):
        return True
    if path.suffix.lower() in EXCLUDE_FILE_SUFFIXES:
        return True
    name = path.name
    if name in (".env",) or name.startswith(".env.") or name.endswith(".env"):
        return False  # flag env files for presence check below
    return False


def scan(root: Path) -> list[dict]:
    findings = []
    files = [p for p in root.rglob("*") if p.is_file() and not _ignored(p)]
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        if path.name == ".env" or path.name.startswith(".env."):
            findings.append({"path": rel, "category": "env_file", "action": "review - never commit"})
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for category, pattern in PATTERNS:
            if category == "email":
                matches = [m for m in pattern.findall(text)
                           if not any(a in m.lower() for a in EMAIL_ALLOWLIST)]
                if matches:
                    findings.append({"path": rel, "category": category, "action": "review"})
                continue
            if pattern.search(text):
                findings.append({"path": rel, "category": category, "action": "review"})
    return findings


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
    print(f"Privacy scan root: {root}")
    findings = scan(root)
    if not findings:
        print("No sensitive markers found.")
        return 0
    # Deduplicate (path, category) and summarize counts per category.
    seen = set()
    unique = []
    for f in findings:
        key = (f["path"], f["category"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    from collections import Counter

    counts = Counter(f["category"] for f in unique)
    print("Findings by category:", dict(counts))
    print("PATH | CATEGORY | ACTION (values are NOT printed)")
    for f in unique:
        print(f"{f['path']} | {f['category']} | {f['action']}")
    print(f"\nTotal unique findings: {len(unique)}")
    print("No files were modified or deleted. Review is advisory only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
