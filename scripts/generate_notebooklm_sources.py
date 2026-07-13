#!/usr/bin/env python3


"""Generate Notebook LM source files from git-tracked code.

One .md file per source directory group. Auto-splits when a group exceeds
~450K characters (buffer under Notebook LM's 500K per-source limit).

Usage:
    python scripts/generate_notebooklm_sources.py [--output-dir notebooklm] [--repo-root .]
"""

import argparse
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

EXPORT_FOLDERS = [
    "api", "core", "db", "frontend", "scripts", "tests", "product-summary", "rhodey_app",
]

EXCLUDE_PATTERNS = {
    ".env", ".env.local", "config.json", "outlook_tokens.json",
    "package-lock.json", "yarn.lock", ".DS_Store",
}

LANGUAGE_MAP = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "typescript",
    ".css": "css", ".html": "html", ".json": "json",
    ".sql": "sql", ".yaml": "yaml", ".yml": "yaml",
    ".md": "markdown", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".toml": "toml", ".txt": "text",
    ".cfg": "ini", ".ini": "ini", ".conf": "ini",
    ".xml": "xml", ".svg": "svg",
    ".rb": "ruby", ".go": "go", ".rs": "rust",
    ".java": "java", ".dart": "dart",
    ".gradle": "groovy", ".kt": "kotlin",
    ".mjs": "javascript", ".cjs": "javascript",
    ".vue": "vue", ".svelte": "svelte",
    ".dockerfile": "dockerfile",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp",
    ".mp3", ".mp4", ".wav", ".ogg", ".mov", ".avi", ".webm",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".eot", ".icns",
    ".o", ".so", ".dylib", ".dll", ".exe",
    ".pyc", ".pyo", ".map",
}

SIZE_LIMIT = 450_000


def get_tracked_files(repo_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=repo_root,
        capture_output=True, text=True, check=True,
    )
    files = []
    for line in result.stdout.strip().splitlines():
        p = Path(line)
        top = p.parts[0]
        if top not in EXPORT_FOLDERS:
            continue
        if any(part in EXCLUDE_PATTERNS for part in p.parts):
            continue
        if p.suffix in BINARY_EXTENSIONS:
            continue
        files.append(p)
    return sorted(files)


def detect_language(path: Path) -> str:
    name = path.name.upper()
    if name == "DOCKERFILE":
        return "dockerfile"
    if name in ("MAKEFILE", "MAKEFILE"):
        return "makefile"
    return LANGUAGE_MAP.get(path.suffix, "")


def read_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
        if "\0" in text:
            return None
        if len(text) > 5_000_000:
            return f"[File too large ({len(text)} bytes) — showing first 5000 chars]\n\n{text[:5000]}"
        return text
    except (UnicodeDecodeError, PermissionError, OSError):
        return None


def git_info(repo_root: Path) -> dict:
    def _run(cmd):
        try:
            return subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "unknown"
    return {
        "commit": _run(["git", "rev-parse", "--short", "HEAD"]),
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "remote": _run(["git", "remote", "get-url", "origin"]),
    }


def estimate_bytes(group: list[Path], repo_root: Path) -> int:
    total = 0
    for f in group:
        try:
            total += os.path.getsize(repo_root / f) + 200
        except OSError:
            total += 5000
    return total


def build_groups(files: list[Path], repo_root: Path) -> dict[str, list[Path]]:
    """Group files by (top_dir, subdir). Root-level files share one group. Split oversized groups."""

    def _group_key(f: Path, depth_components: int) -> str:
        top = f.parts[0]
        if len(f.parts) <= depth_components:
            return f"{top}/_root"
        parts = f.parts[1:depth_components]
        if not parts:
            return f"{top}/_root"
        return f"{top}/{'/'.join(parts)}"

    def _initial_groups():
        groups: dict[str, list[Path]] = {}
        for f in files:
            groups.setdefault(_group_key(f, 2), []).append(f)
        return groups

    def _split_group(key: str, group: list[Path], depth: int) -> dict[str, list[Path]]:
        if depth >= 5:
            return {key: group}
        est = estimate_bytes(group, repo_root)
        if est <= SIZE_LIMIT * 1.5:
            return {key: group}

        result: dict[str, list[Path]] = {}
        sub: dict[str, list[Path]] = {}
        for f in group:
            sub.setdefault(_group_key(f, depth), []).append(f)
        for sk, sg in sub.items():
            result.update(_split_group(sk, sg, depth + 1))
        return result

    result: dict[str, list[Path]] = {}
    for key, group in _initial_groups().items():
        result.update(_split_group(key, group, 3))
    return result


def output_name(group_key: str) -> str:
    name = group_key.replace("/", "-")  # "core-pulse", "frontend-src-app"
    if name.endswith("-_root"):
        name = name[:-6]  # "core-_root" → "core"
    return name + ".md"


def display_dir(group_key: str) -> str:
    dirname = group_key.replace("_root", "").rstrip("/")
    return dirname + "/"


def write_source_file(dest: Path, group_key: str, files: list[Path], repo_root: Path):
    info = git_info(repo_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        f"# {display_dir(group_key)} — Codebase Source Export",
        "",
        f"**Generated:** {timestamp}",
        f"**Commit:** `{info['commit']}`",
        f"**Branch:** `{info['branch']}`",
        f"**Files:** {len(files)}",
        "",
        "---",
        "",
    ]

    for f in files:
        content = read_file(repo_root / f)
        if content is None:
            continue
        lang = detect_language(f)
        lines.append(f"## {f}")
        lines.append("")
        if lang == "markdown":
            lines.append(content)
        elif lang:
            lines.append(f"```{lang}")
            lines.append(content)
            lines.append("```")
        else:
            lines.append("```")
            lines.append(content)
            lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    dest.write_text("\n".join(lines), encoding="utf-8")


def write_index(dest: Path, repo_root: Path, output_files: list[str]):
    info = git_info(repo_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "# Integrated-OS — Notebook LM Source Index",
        "",
        f"**Generated:** {timestamp}",
        f"**Commit:** `{info['commit']}`",
        f"**Branch:** `{info['branch']}`",
        f"**Remote:** `{info['remote']}`",
        "",
        "## Files",
        "",
    ]
    for name in sorted(output_files):
        lines.append(f"- [{name}]({name})")
    lines.append("")
    dest.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Generate Notebook LM source files")
    parser.add_argument("--output-dir", default="notebooklm", help="Output directory (default: notebooklm)")
    parser.add_argument("--repo-root", default=".", help="Repository root path (default: .)")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = repo_root / output_dir

    output_dir.mkdir(parents=True, exist_ok=True)

    # Clean old files so stale ones don't linger
    for old in output_dir.glob("*.md"):
        old.unlink()

    print(f"Scanning git-tracked files in {repo_root}...")
    files = get_tracked_files(repo_root)
    print(f"  Found {len(files)} tracked files in export folders")

    groups = build_groups(files, repo_root)
    output_names = []

    for key in sorted(groups, key=lambda k: (k.split("/")[0], k)):
        group = groups[key]
        name = output_name(key)
        path = output_dir / name
        print(f"  {name} ({len(group)} files)")
        write_source_file(path, key, group, repo_root)
        output_names.append(name)

    write_index(output_dir / "_index.md", repo_root, output_names)

    total_refs = sum(len(v) for v in groups.values())
    print(f"\nDone — {len(groups)} source files generated ({total_refs} file references)")
    print(f"Output: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
