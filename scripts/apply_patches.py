#!/usr/bin/env python3
"""Project Mu (無) - apply the Mu patch set to an unpacked Chromium tree.

Reads patches/patch_order.list and brings src/ in line with it:

  * patches already applied (same content) are skipped,
  * patches that were edited or removed from the list are reverted first,
  * remaining patches are applied in order; the first failure halts the run
    with the failing file, line, hunk and current source text.

Applied patches are recorded (with a copy of each patch) in src/.mu/, so the
engine can always revert exactly what it applied, and reruns are safe.

    python scripts/apply_patches.py --workdir C:/mu-build      # apply / sync
    python scripts/apply_patches.py --workdir C:/mu-build --revert
    python scripts/apply_patches.py --workdir C:/mu-build --status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_DIR = REPO_ROOT / "patches"
ORDER_FILE = PATCH_DIR / "patch_order.list"
STATE_DIR = ".mu"
STATE_FILE = "applied.json"
VERSION_MARKER = ".mu_version"

GIT_FLAGS = ["--ignore-space-change", "--ignore-whitespace", "--whitespace=nowarn", "-p1"]
# Byte-exact I/O: Git for Windows ships core.autocrlf=true system-wide, and
# `git apply` honours it even outside a repo, silently rewriting LF files as CRLF.
GIT_CONFIG = ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.safecrlf=false"]
# --binary stops Windows builds of GNU patch from stripping CRs in CRLF sources.
PATCH_FLAGS = ["-p1", "--batch", "--forward", "--ignore-whitespace", "--no-backup-if-mismatch",
               "--binary", "-r", "-"]


class PatchError(RuntimeError):
    pass


def log(msg: str = "") -> None:
    print(f"[mu] {msg}" if msg else "", flush=True)


@dataclass(frozen=True)
class Patch:
    name: str
    path: Path
    sha256: str


# --------------------------------------------------------------------------
# Patch list
# --------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_order(order_file: Path, patch_dir: Path) -> list[Patch]:
    if not order_file.is_file():
        raise PatchError(f"patch list not found: {order_file}")
    patches: list[Patch] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(order_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{order_file.name}:{lineno}"
        if "/" in line or "\\" in line or line in (".", ".."):
            raise PatchError(f"{where}: entries must be bare file names in patches/, got {line!r}")
        if not line.endswith(".patch"):
            raise PatchError(f"{where}: {line!r} does not end in .patch")
        if line in seen:
            raise PatchError(f"{where}: {line!r} is listed twice")
        path = patch_dir / line
        if not path.is_file():
            raise PatchError(f"{where}: {line!r} does not exist in {patch_dir}")
        if path.stat().st_size == 0:
            raise PatchError(f"{where}: {line!r} is empty")
        # Content lines may legitimately carry CRLF (patching a CRLF source file),
        # but CRLF on the diff's own header lines means the file was converted.
        if any(ln.startswith((b"--- ", b"+++ ", b"@@ ")) and ln.endswith(b"\r")
               for ln in path.read_bytes().split(b"\n")):
            raise PatchError(f"{where}: {line!r} has CRLF line endings; "
                             "patches must be LF (see .gitattributes)")
        seen.add(line)
        patches.append(Patch(line, path, sha256_of(path)))
    return patches


# --------------------------------------------------------------------------
# Applied-state bookkeeping (src/.mu/)
# --------------------------------------------------------------------------

class State:
    def __init__(self, src: Path):
        self.dir = src / STATE_DIR
        self.file = self.dir / STATE_FILE
        self.applied: list[dict] = []
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                self.applied = list(data["applied"])
            except (ValueError, KeyError, TypeError) as e:
                raise PatchError(f"corrupt state file {self.file}: {e}") from e

    def save(self) -> None:
        self.dir.mkdir(exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"applied": self.applied}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.file)

    def stored_copy(self, name: str) -> Path:
        return self.dir / name

    def push(self, patch: Patch) -> None:
        self.dir.mkdir(exist_ok=True)
        shutil.copyfile(patch.path, self.stored_copy(patch.name))
        self.applied.append({"name": patch.name, "sha256": patch.sha256})
        self.save()

    def pop(self) -> None:
        entry = self.applied.pop()
        self.save()
        self.stored_copy(entry["name"]).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

def tool_env(src: Path) -> dict[str, str]:
    env = dict(os.environ)
    # Stop git from discovering an enclosing repository (e.g. project-mu/ when
    # src/ lives inside it). Inside a repo, `git apply` run from a subdirectory
    # silently ignores paths outside it — patches would "succeed" doing nothing.
    if not (src / ".git").exists():
        env["GIT_CEILING_DIRECTORIES"] = str(src.resolve().parent)
    env["LC_ALL"] = "C"  # stable, English tool output for parsing
    return env


def run(cmd: list[str], src: Path, patch_file: Path | None = None) -> subprocess.CompletedProcess:
    stdin = patch_file.read_bytes() if patch_file else None
    return subprocess.run(cmd, cwd=src, input=stdin, capture_output=True, env=tool_env(src))


def decode(b: bytes) -> str:
    return b.decode("utf-8", "replace").strip()


class Backend:
    name = ""

    def check(self, src: Path, patch: Path, reverse: bool = False) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def apply(self, src: Path, patch: Path, reverse: bool = False) -> subprocess.CompletedProcess:
        raise NotImplementedError


class GitBackend(Backend):
    name = "git apply"

    def _cmd(self, extra: list[str], reverse: bool) -> list[str]:
        return ["git", *GIT_CONFIG, "apply", *GIT_FLAGS, *(["--reverse"] if reverse else []), *extra]

    def check(self, src, patch, reverse=False):
        return run(self._cmd(["--check", "-v", str(patch.resolve())], reverse), src)

    def apply(self, src, patch, reverse=False):
        return run(self._cmd([str(patch.resolve())], reverse), src)


class PatchBackend(Backend):
    """GNU patch. Not atomic, so apply() is always preceded by a dry run."""
    name = "patch"

    def _cmd(self, extra: list[str], reverse: bool) -> list[str]:
        return ["patch", *PATCH_FLAGS, *(["--reverse"] if reverse else []), *extra]

    def check(self, src, patch, reverse=False):
        return run(self._cmd(["--dry-run"], reverse), src, patch)

    def apply(self, src, patch, reverse=False):
        return run(self._cmd([], reverse), src, patch)


def pick_backend(choice: str) -> Backend:
    have_git, have_patch = shutil.which("git"), shutil.which("patch")
    if choice == "git" or (choice == "auto" and have_git):
        if not have_git:
            raise PatchError("git not found on PATH")
        return GitBackend()
    if not have_patch:
        raise PatchError("neither git nor GNU patch found on PATH")
    return PatchBackend()


# --------------------------------------------------------------------------
# Failure diagnostics
# --------------------------------------------------------------------------

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
GIT_FAIL_RE = re.compile(r"error: patch failed: (.+):(\d+)")
GIT_MISSING_RE = re.compile(r"error: (.+): (?:No such file or directory|does not exist in index)")
PATCH_FILE_RE = re.compile(r"^(?:patching|checking) file '?(.+?)'?$", re.M)
PATCH_HUNK_RE = re.compile(r"Hunk #(\d+) FAILED at (\d+)")


def parse_hunks(patch_text: str) -> dict[str, list[tuple[int, list[str]]]]:
    """Map target file -> [(old_start_line, hunk_lines)]."""
    files: dict[str, list[tuple[int, list[str]]]] = {}
    current: list[tuple[int, list[str]]] | None = None
    hunk: list[str] | None = None
    for line in patch_text.splitlines():
        if line.startswith("--- "):
            hunk = None
            continue
        if line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            if target.startswith("b/"):
                target = target[2:]
            current = files.setdefault(target, [])
            hunk = None
            continue
        m = HUNK_RE.match(line)
        if m and current is not None:
            hunk = [line]
            current.append((int(m.group(1)), hunk))
            continue
        if line.startswith("diff "):
            current, hunk = None, None
            continue
        if hunk is not None and line[:1] in (" ", "+", "-", "\\"):
            hunk.append(line)
    return files


def show_source(src: Path, rel: str, line: int, span: int) -> None:
    path = src / rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        log(f"  {rel} does not exist in src/ - upstream probably moved or deleted it")
        return
    lo, hi = max(line - 3, 1), min(line + span + 3, len(text))
    log(f"  current upstream {rel} (lines {lo}-{hi}):")
    for i in range(lo, hi + 1):
        print(f"      {i:6d} | {text[i - 1]}")


def diagnose(backend: Backend, src: Path, patch: Patch, result: subprocess.CompletedProcess) -> None:
    output = decode(result.stdout) + "\n" + decode(result.stderr)
    log(f"{backend.name} output:")
    for line in output.strip().splitlines():
        print(f"      {line}")

    hunks = parse_hunks(patch.path.read_text(encoding="utf-8", errors="replace"))
    failures: list[tuple[str, int, int | None]] = []  # (file, line, hunk#)
    for m in GIT_FAIL_RE.finditer(output):
        failures.append((m.group(1).strip(), int(m.group(2)), None))
    for m in GIT_MISSING_RE.finditer(output):
        failures.append((m.group(1).strip(), 0, None))
    current_file = None
    for line in output.splitlines():
        fm = PATCH_FILE_RE.match(line.strip())
        if fm:
            current_file = fm.group(1)
        hm = PATCH_HUNK_RE.search(line)
        if hm and current_file:
            failures.append((current_file, int(hm.group(2)), int(hm.group(1))))

    if not failures:
        log("could not locate the failing hunk automatically; see tool output above")
        return
    for rel, line, hunk_no in failures:
        log()
        log(f"FAILED: {rel}" + (f" at line {line}" if line else ""))
        file_hunks = hunks.get(rel, [])
        hunk = None
        if hunk_no is not None and 0 < hunk_no <= len(file_hunks):
            hunk = file_hunks[hunk_no - 1]
        elif file_hunks:
            # git reports the hunk's old start line; fall back to the closest one.
            hunk = min(file_hunks, key=lambda h: abs(h[0] - line))
        if hunk:
            log(f"  expected by {patch.name}:")
            for hl in hunk[1]:
                print(f"      {hl}")
            if line:
                show_source(src, rel, hunk[0], sum(1 for h in hunk[1][1:] if h[:1] in " -"))
        elif line == 0:
            show_source(src, rel, 1, 0)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def revert_one(backend: Backend, src: Path, state: State) -> None:
    entry = state.applied[-1]
    stored = state.stored_copy(entry["name"])
    if not stored.is_file():
        raise PatchError(f"cannot revert {entry['name']}: stored copy {stored} is missing; "
                         "re-fetch a clean tree with fetch_upstream.py --force")
    res = backend.check(src, stored, reverse=True)
    if res.returncode != 0:
        log(f"cannot cleanly revert {entry['name']} (was src/ edited by hand?)")
        diagnose(backend, src, Patch(entry["name"], stored, entry["sha256"]), res)
        raise PatchError(f"revert of {entry['name']} failed; re-fetch a clean tree with "
                         "fetch_upstream.py --force")
    res = backend.apply(src, stored, reverse=True)
    if res.returncode != 0:
        raise PatchError(f"revert of {entry['name']} failed after a clean check:\n"
                         f"{decode(res.stderr) or decode(res.stdout)}")
    state.pop()
    log(f"reverted  {entry['name']}")


def sync(backend: Backend, src: Path, patches: list[Patch], state: State) -> int:
    # Longest prefix of the applied stack that still matches the wanted list.
    keep = 0
    for applied, wanted in zip(state.applied, patches):
        if applied["name"] != wanted.name or applied["sha256"] != wanted.sha256:
            break
        keep += 1
    stale = len(state.applied) - keep
    if stale:
        log(f"{stale} applied patch(es) changed or were removed from the list; reverting")
        while len(state.applied) > keep:
            revert_one(backend, src, state)

    for p in patches[:keep]:
        log(f"unchanged {p.name}")

    applied_now = 0
    for p in patches[keep:]:
        res = backend.check(src, p.path)
        if res.returncode != 0:
            log()
            log(f"patch {p.name} does not apply to this Chromium tree")
            diagnose(backend, src, p, res)
            log()
            raise PatchError(f"halted at {p.name}; {applied_now} patch(es) applied this run, "
                             f"{len(state.applied)} total. Fix or rebase the patch, then rerun.")
        res = backend.apply(src, p.path)
        if res.returncode != 0:
            raise PatchError(f"{p.name} passed --check but failed to apply:\n"
                             f"{decode(res.stderr) or decode(res.stdout)}")
        state.push(p)
        applied_now += 1
        log(f"applied   {p.name}")
    return applied_now


def status(src: Path, patches: list[Patch], state: State) -> None:
    applied = {e["name"]: e["sha256"] for e in state.applied}
    for p in patches:
        if p.name not in applied:
            mark = "pending"
        elif applied[p.name] != p.sha256:
            mark = "STALE (edited since applied)"
        else:
            mark = "applied"
        log(f"{mark:<29} {p.name}")
    wanted = {p.name for p in patches}
    for e in state.applied:
        if e["name"] not in wanted:
            log(f"{'ORPHAN (no longer listed)':<29} {e['name']}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Apply the Project Mu patch set to src/.")
    ap.add_argument("--workdir", type=Path,
                    default=Path(os.environ.get("MU_WORKDIR", REPO_ROOT)),
                    help="folder containing src/ (env: MU_WORKDIR; default: repo root)")
    ap.add_argument("--src", type=Path, help="Chromium source dir (default: <workdir>/src)")
    ap.add_argument("--patch-dir", type=Path, default=PATCH_DIR, help=argparse.SUPPRESS)
    ap.add_argument("--tool", choices=["auto", "git", "patch"], default="auto",
                    help="patch backend (default: git if available)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--revert", action="store_true", help="revert every applied Mu patch")
    mode.add_argument("--status", action="store_true", help="show patch state and exit")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    src: Path = (args.src or args.workdir / "src").resolve()
    patch_dir: Path = args.patch_dir.resolve()

    if not src.is_dir():
        raise PatchError(f"source tree not found: {src} (run fetch_upstream.py first, "
                         "with the same --workdir)")
    version_file = src / VERSION_MARKER
    version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "?"
    if version == "?":
        log(f"warning: {src} has no {VERSION_MARKER}; was it created by fetch_upstream.py?")

    patches = read_order(patch_dir / ORDER_FILE.name, patch_dir)
    state = State(src)

    if args.status:
        log(f"Chromium {version} at {src}")
        status(src, patches, state)
        return 0

    backend = pick_backend(args.tool)
    log(f"Chromium {version} at {src} - using {backend.name}")

    if args.revert:
        if not state.applied:
            log("no Mu patches are applied; nothing to revert")
            return 0
        while state.applied:
            revert_one(backend, src, state)
        log("all Mu patches reverted")
        return 0

    if not patches:
        log("patch_order.list has no active entries")
    n = sync(backend, src, patches, state)
    log(f"done: {n} applied this run, {len(state.applied)}/{len(patches)} active")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    try:
        sys.exit(main())
    except PatchError as e:
        log(f"error: {e}")
        sys.exit(1)
