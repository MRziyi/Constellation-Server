"""fs adapter — local filesystem (Twin-adjacent reads, project-scoped writes).

Actions: read, write, append, grep, list, delete.

Per TOOL-ADAPTERS.md §5 + Zack's note (2026-05-24) that the biggest single use is
"让 Claude Code 去不同目录里看东西" — Cortex can pre-read READMEs / file lists / grep
results, then dispatch claude_code with the relevant working_dir + context.

Safety model:
- **Reads** (read / list / grep) — anywhere on the filesystem. No restriction; user-driven.
- **Writes** (write / append) — restricted to whitelisted roots:
    * ~/constellation/  (Twin and adjacent)
    * ~/Code/Projects/  (Zack's project dirs)
    * /tmp/             (scratch)
  Writes outside these raise immediately.
- **Delete** — restricted to ~/constellation/twin/ AND non-recursive only. Anything else
  raises. This is intentionally tight; if Zack needs broader delete, prefer rm via Claude
  Code with explicit per-call confirmation.

Per confirm-policies.md:
  fs:read    → auto
  fs:list    → auto
  fs:grep    → auto
  fs:append  → auto (append-only is safer than write)
  fs:write   → preview-always (HUD gates regardless)
  fs:delete  → preview-always
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any


# Writable roots — expansion happens at runtime against $HOME.
_WRITE_ROOTS = [
    Path.home() / "constellation",
    Path.home() / "Code" / "Projects",
    Path("/tmp"),
]

# Delete is even tighter: only Twin.
_DELETE_ROOTS = [Path.home() / "constellation" / "twin"]


def _expand(path_str: str) -> Path:
    return Path(os.path.expanduser(path_str)).resolve()


def _is_under(child: Path, roots: list[Path]) -> bool:
    try:
        child_resolved = child.resolve()
    except OSError:
        # Non-existent path: resolve parents incrementally.
        child_resolved = child
    for root in roots:
        try:
            child_resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


class FsAdapter:
    name = "fs"

    SIDE_EFFECT_ACTIONS = {"write", "append", "delete"}

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        # write / delete / append refuse `draft` mode (no draft semantics — the action either
        # touches the file or doesn't). Router must use execute + (for write/delete) preview.
        if action in self.SIDE_EFFECT_ACTIONS and result_format == "draft":
            raise ValueError(
                f"fs.{action} has no draft semantics. Router must use result_format='execute'."
            )

        if action == "read":
            return await self._read(args)
        if action == "list":
            return await self._list(args)
        if action == "grep":
            return await self._grep(args)
        if action == "write":
            return await self._write(args)
        if action == "append":
            return await self._append(args)
        if action == "delete":
            return await self._delete(args)
        raise ValueError(f"fs: unknown action '{action}'")

    # ── reads ──

    async def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        path_str = args.get("path")
        if not path_str:
            raise ValueError("fs.read: 'path' required")
        p = _expand(path_str)
        if not p.exists():
            return {"path": str(p), "exists": False}
        if not p.is_file():
            raise ValueError(f"fs.read: {p} is not a regular file")
        max_bytes = int(args.get("max_bytes", 200_000))
        stat = p.stat()
        if stat.st_size > max_bytes:
            content = p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
            truncated = True
        else:
            content = p.read_text(encoding="utf-8", errors="replace")
            truncated = False
        return {
            "path": str(p),
            "exists": True,
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
            "content": content,
            "truncated": truncated,
        }

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        path_str = args.get("path") or "."
        glob = args.get("glob") or "*"
        recursive = bool(args.get("recursive", False))
        max_items = int(args.get("max_items", 500))

        p = _expand(path_str)
        if not p.is_dir():
            raise ValueError(f"fs.list: {p} is not a directory")
        iterator = p.rglob(glob) if recursive else p.glob(glob)
        items: list[dict[str, Any]] = []
        for child in iterator:
            if len(items) >= max_items:
                break
            try:
                stat = child.stat()
                items.append({
                    "path": str(child),
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "size_bytes": stat.st_size if child.is_file() else 0,
                })
            except OSError:
                continue
        items.sort(key=lambda x: (not x["is_dir"], x["name"]))
        return {"path": str(p), "glob": glob, "recursive": recursive, "count": len(items), "items": items}

    async def _grep(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = args.get("pattern")
        paths = args.get("paths") or args.get("path")
        if not pattern:
            raise ValueError("fs.grep: 'pattern' required")
        if not paths:
            raise ValueError("fs.grep: 'paths' (list or single) required")
        if isinstance(paths, str):
            paths = [paths]
        case_sensitive = bool(args.get("case_sensitive", False))
        is_regex = bool(args.get("regex", True))
        max_matches = int(args.get("max_matches", 200))
        # Default-exclude noise that dominates results in typical repos. Override via
        # args.include_vendored=true if you really want to search inside dependencies.
        include_vendored = bool(args.get("include_vendored", False))
        default_excludes = [
            ".venv", "node_modules", ".git", "__pycache__", ".pytest_cache",
            ".mypy_cache", ".ruff_cache", "dist", "build", ".next", ".nuxt",
        ]

        # Prefer ripgrep if present; fall back to grep.
        rg = "rg" if await _which("rg") else None
        cmd: list[str]
        if rg:
            cmd = [rg, "--no-heading", "--line-number", "--max-count", str(max_matches), "--color=never"]
            if not case_sensitive:
                cmd.append("-i")
            if not is_regex:
                cmd.append("-F")
            if not include_vendored:
                for d in default_excludes:
                    cmd.extend(["-g", f"!{d}"])
            cmd.append(pattern)
            cmd.extend(str(_expand(p)) for p in paths)
        else:
            cmd = ["grep", "-r", "-n"]
            if not case_sensitive:
                cmd.append("-i")
            if not is_regex:
                cmd.append("-F")
            else:
                cmd.append("-E")
            if not include_vendored:
                for d in default_excludes:
                    cmd.extend(["--exclude-dir", d])
            cmd.append(pattern)
            cmd.extend(str(_expand(p)) for p in paths)

        rc, stdout, stderr = await _run(cmd)
        # Both rg and grep exit 1 if nothing found; that's not an error.
        if rc not in (0, 1):
            raise RuntimeError(f"grep failed (rc={rc}): {stderr.strip() or 'unknown'}")
        matches = []
        for line in stdout.splitlines():
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            matches.append({"path": parts[0], "line_no": int(parts[1]) if parts[1].isdigit() else -1, "line": parts[2]})
            if len(matches) >= max_matches:
                break
        return {"pattern": pattern, "count": len(matches), "matches": matches, "engine": rg or "grep"}

    # ── writes ──

    async def _write(self, args: dict[str, Any]) -> dict[str, Any]:
        path_str = args.get("path")
        # Accept 'content' (canonical) or 'text'/'body' (Router sometimes
        # generates these aliases). Same for append().
        content = args.get("content")
        if content is None:
            content = args.get("text") or args.get("body")
        if not path_str or content is None:
            raise ValueError("fs.write: 'path' and 'content' required")
        mode = args.get("mode", "overwrite")
        if mode not in ("overwrite", "create_only"):
            raise ValueError(f"fs.write: unknown mode '{mode}'")
        p = _expand(path_str)
        if not _is_under(p, _WRITE_ROOTS):
            raise PermissionError(
                f"fs.write: {p} is outside writable roots {[str(r) for r in _WRITE_ROOTS]}"
            )
        if mode == "create_only" and p.exists():
            raise FileExistsError(f"fs.write: {p} exists; mode=create_only refuses overwrite")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": str(p), "bytes_written": len(content.encode("utf-8")), "mode": mode}

    async def _append(self, args: dict[str, Any]) -> dict[str, Any]:
        path_str = args.get("path")
        content = args.get("content")
        if not path_str or content is None:
            raise ValueError("fs.append: 'path' and 'content' required")
        p = _expand(path_str)
        if not _is_under(p, _WRITE_ROOTS):
            raise PermissionError(
                f"fs.append: {p} is outside writable roots {[str(r) for r in _WRITE_ROOTS]}"
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return {"path": str(p), "bytes_appended": len(content.encode("utf-8"))}

    async def _delete(self, args: dict[str, Any]) -> dict[str, Any]:
        path_str = args.get("path")
        if not path_str:
            raise ValueError("fs.delete: 'path' required")
        recursive = bool(args.get("recursive", False))
        p = _expand(path_str)
        if not _is_under(p, _DELETE_ROOTS):
            raise PermissionError(
                f"fs.delete: {p} is outside delete-allowed roots {[str(r) for r in _DELETE_ROOTS]}."
                f" v1 only allows delete inside ~/constellation/twin/."
            )
        if not p.exists():
            return {"path": str(p), "deleted": False, "reason": "not_found"}
        if p.is_dir():
            if not recursive:
                raise ValueError(f"fs.delete: {p} is a directory; pass recursive=true to remove")
            import shutil
            shutil.rmtree(p)
            return {"path": str(p), "deleted": True, "kind": "directory", "recursive": True}
        p.unlink()
        return {"path": str(p), "deleted": True, "kind": "file"}


async def _which(name: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "which", name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    out = out.decode().strip()
    return out or None
