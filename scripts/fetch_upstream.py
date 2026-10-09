#!/usr/bin/env python3
"""Project Mu (無) - fetch the upstream Chromium source tarball.

Resolves the latest Chromium release for a channel/platform via the Chromium
Dash API, downloads the official source tarball (resumable, SHA-256 verified),
and streams it into a source directory without loading it into memory.

Standard library only. Usage:

    python scripts/fetch_upstream.py                       # latest Stable -> ./src
    python scripts/fetch_upstream.py --dry-run             # resolve + probe only
    python scripts/fetch_upstream.py --version 156.0.8078.12 --workdir D:/mu
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

DASH_URL = (
    "https://chromiumdash.appspot.com/fetch_releases"
    "?channel={channel}&platform={platform}&num=1"
)
TARBALL_URL = (
    "https://commondatastorage.googleapis.com/chromium-browser-official/"
    "chromium-{version}.tar.xz"
)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
VERSION_MARKER = ".mu_version"
REPO_ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "project-mu-fetcher/1.0"
CHUNK = 1024 * 1024
HTTP_TIMEOUT = 60
MAX_RETRIES = 8
# Unpacked Chromium is roughly 6-8x the compressed tarball.
UNPACK_RATIO = 8


class FetchError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[mu] {msg}", flush=True)


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def fs_path(path: Path) -> str:
    """Return a path string safe for very long paths on Windows."""
    s = str(path.resolve())
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return s


def under(base: str, rel: PurePosixPath) -> str:
    """Join an archive-relative path onto an fs_path() base without re-resolving."""
    return os.path.join(base, *rel.parts) if rel.parts else base


def onedrive_roots() -> list[Path]:
    roots = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        val = os.environ.get(var)
        if val:
            roots.append(Path(val).resolve())
    return roots


def inside_onedrive(path: Path) -> bool:
    resolved = path.resolve()
    return any(resolved == r or r in resolved.parents for r in onedrive_roots())


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_open(url: str, headers: dict[str, str] | None = None, method: str = "GET"):
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)


def with_retries(what: str, fn):
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            # 4xx (other than throttling) will not fix itself.
            if 400 <= e.code < 500 and e.code not in (408, 429):
                raise FetchError(f"{what}: HTTP {e.code} {e.reason}") from e
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            err = str(e)
        if attempt == MAX_RETRIES:
            raise FetchError(f"{what}: giving up after {MAX_RETRIES} attempts ({err})")
        log(f"{what}: {err} - retry {attempt}/{MAX_RETRIES - 1} in {delay:.0f}s")
        time.sleep(delay)
        delay = min(delay * 2, 60)


def resolve_version(channel: str, platform: str) -> str:
    url = DASH_URL.format(channel=channel, platform=platform)

    def fetch():
        with http_open(url) as r:
            return json.load(r)

    releases = with_retries("Chromium Dash", fetch)
    if not isinstance(releases, list) or not releases:
        raise FetchError(f"Chromium Dash returned no {channel}/{platform} releases")
    version = releases[0].get("version", "")
    if not VERSION_RE.match(version):
        raise FetchError(f"Chromium Dash returned an unexpected version: {version!r}")
    return version


def remote_size(url: str) -> int:
    def head():
        with http_open(url, method="HEAD") as r:
            return int(r.headers.get("Content-Length", "0"))

    size = with_retries("tarball HEAD", head)
    if size <= 0:
        raise FetchError(f"server did not report a size for {url}")
    return size


def expected_sha256(url: str) -> str | None:
    """Parse '<algo>  <hex>  <file>' lines from the published .hashes file."""
    def fetch():
        with http_open(url + ".hashes") as r:
            return r.read().decode("utf-8", "replace")

    try:
        text = with_retries("hashes file", fetch)
    except FetchError as e:
        log(f"warning: no published hashes ({e}); integrity check skipped")
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == "sha256":
            return parts[1].lower()
    log("warning: hashes file has no sha256 entry; integrity check skipped")
    return None


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------

def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_eta(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Progress:
    """Single-line progress bar on a TTY, periodic log lines otherwise."""

    def __init__(self, label: str, total: int, start: int = 0):
        self.label, self.total, self.done = label, total, start
        self.base, self.t0 = start, time.monotonic()
        self.last_draw, self.last_pct_logged = 0.0, -1
        self.tty = sys.stderr.isatty()

    def update(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if self.tty and now - self.last_draw < 0.2 and self.done < self.total:
            return
        self.last_draw = now
        pct = self.done / self.total * 100 if self.total else 0.0
        elapsed = max(now - self.t0, 1e-6)
        speed = (self.done - self.base) / elapsed
        eta = (self.total - self.done) / speed if speed > 0 else 0
        if self.tty:
            width = 30
            filled = int(width * min(pct, 100) / 100)
            bar = "█" * filled + "░" * (width - filled)
            sys.stderr.write(
                f"\r[mu] {self.label} {bar} {pct:5.1f}%  "
                f"{fmt_bytes(self.done)}/{fmt_bytes(self.total)}  "
                f"{fmt_bytes(speed)}/s  ETA {fmt_eta(eta)}   "
            )
            sys.stderr.flush()
        elif int(pct) // 5 != self.last_pct_logged:
            self.last_pct_logged = int(pct) // 5
            log(f"{self.label} {pct:5.1f}%  {fmt_bytes(self.done)}/{fmt_bytes(self.total)}"
                f"  {fmt_bytes(speed)}/s")

    def close(self) -> None:
        if self.tty:
            sys.stderr.write("\n")
            sys.stderr.flush()


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    size = path.stat().st_size
    prog = Progress("verify  ", size)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK * 8):
            h.update(chunk)
            prog.update(len(chunk))
    prog.close()
    return h.hexdigest()


def download(url: str, dest: Path, size: int, sha256: str | None) -> None:
    """Resumable download into dest (via dest.part), verified before rename."""
    if dest.exists() and dest.stat().st_size == size:
        if sha256 is None or sha256_file(dest) == sha256:
            log(f"archive already downloaded: {dest}")
            return
        log("existing archive failed verification; re-downloading")
        dest.unlink()

    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if part.exists() and part.stat().st_size > size:
        part.unlink()

    def attempt() -> None:
        have = part.stat().st_size if part.exists() else 0
        if have == size:
            return
        headers = {"Range": f"bytes={have}-"} if have else {}
        with http_open(url, headers=headers) as r:
            if have and r.status != 206:
                log("server ignored resume request; restarting from zero")
                have = 0
            mode = "ab" if have else "wb"
            prog = Progress("download", size, have)
            try:
                with open(part, mode) as f:
                    while chunk := r.read(CHUNK):
                        f.write(chunk)
                        prog.update(len(chunk))
            finally:
                prog.close()
        got = part.stat().st_size
        if got != size:
            raise ConnectionError(f"incomplete download ({got}/{size} bytes)")

    with_retries("download", attempt)

    if sha256 is not None:
        actual = sha256_file(part)
        if actual != sha256:
            part.unlink()
            raise FetchError(f"SHA-256 mismatch: expected {sha256}, got {actual} "
                             "(corrupt file deleted; rerun to download again)")
        log("SHA-256 verified")
    os.replace(part, dest)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

class CountingReader:
    """File wrapper that reports compressed bytes consumed to a Progress."""

    def __init__(self, f, prog: Progress):
        self.f, self.prog = f, prog

    def read(self, n: int = -1) -> bytes:
        data = self.f.read(n)
        self.prog.update(len(data))
        return data


def safe_relpath(name: str, prefix: str) -> PurePosixPath | None:
    """Strip the 'chromium-<ver>/' prefix; reject absolute or escaping paths."""
    p = PurePosixPath(name)
    parts = p.parts
    if not parts or parts[0] != prefix:
        raise FetchError(f"unexpected archive entry outside {prefix}/: {name!r}")
    rel = parts[1:]
    if not rel:
        return None  # the top-level directory itself
    if any(part in ("..", "") for part in rel) or p.is_absolute():
        raise FetchError(f"refusing unsafe archive path: {name!r}")
    return PurePosixPath(*rel)


def link_target(rel: PurePosixPath, member: tarfile.TarInfo) -> PurePosixPath | None:
    """Resolve a link's target to a path inside the tree, or None if it escapes."""
    if PurePosixPath(member.linkname).is_absolute():
        return None
    if member.issym():
        target = rel.parent / member.linkname
    else:  # hard links are archive-root relative, including the prefix
        target = PurePosixPath(*PurePosixPath(member.linkname).parts[1:])
    resolved: list[str] = []
    for part in target.parts:
        if part == "..":
            if not resolved:
                return None
            resolved.pop()
        elif part not in (".", ""):
            resolved.append(part)
    return PurePosixPath(*resolved) if resolved else None


def materialize_links(root: str, links: list[tuple[PurePosixPath, tarfile.TarInfo]]) -> None:
    """Create deferred links: real links where allowed, otherwise copies."""
    pending = list(links)
    skipped = 0
    # Links can point at other links; loop until no further progress.
    while pending:
        progress = False
        remaining = []
        for rel, member in pending:
            dst = under(root, rel)
            tgt_rel = link_target(rel, member)
            if tgt_rel is None or rel == tgt_rel or tgt_rel in rel.parents:
                log(f"warning: skipping link that escapes or loops: {rel} -> {member.linkname}")
                skipped += 1
                progress = True
                continue
            src = under(root, tgt_rel)
            if not os.path.lexists(src):
                remaining.append((rel, member))
                continue
            progress = True
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.lexists(dst):
                if os.path.isdir(dst) and not os.path.islink(dst):
                    shutil.rmtree(dst)
                else:
                    os.unlink(dst)
            try:
                if member.issym():
                    os.symlink(member.linkname.replace("/", os.sep), dst,
                               target_is_directory=os.path.isdir(src))
                else:
                    os.link(src, dst)
                continue
            except OSError:
                pass  # e.g. Windows without Developer Mode — fall back to a copy
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst, symlinks=False)
                else:
                    shutil.copy2(src, dst)
            except (OSError, shutil.Error) as e:
                log(f"warning: could not copy link {rel} -> {member.linkname}: {e}")
                skipped += 1
        if not progress:
            for rel, member in remaining:
                log(f"warning: dangling link left out: {rel} -> {member.linkname}")
            skipped += len(remaining)
            break
        pending = remaining
    if skipped:
        log(f"{skipped} link(s) could not be created (see warnings above)")


def extract(archive: Path, dest: Path, version: str) -> None:
    """Stream-extract archive into dest atomically (via a .partial directory)."""
    prefix = f"chromium-{version}"
    staging = dest.with_name(dest.name + ".partial")
    if staging.exists():
        log(f"removing stale staging dir {staging}")
        shutil.rmtree(fs_path(staging))
    base = fs_path(staging)
    os.makedirs(base)

    links: list[tuple[PurePosixPath, tarfile.TarInfo]] = []
    files = 0
    prog = Progress("unpack  ", archive.stat().st_size)
    try:
        with open(archive, "rb") as raw:
            # "r|xz" is a forward-only stream: constant memory, no seeking.
            with tarfile.open(fileobj=CountingReader(raw, prog), mode="r|xz") as tar:
                for member in tar:
                    rel = safe_relpath(member.name, prefix)
                    if rel is None:
                        continue
                    target = under(base, rel)
                    if member.isdir():
                        os.makedirs(target, exist_ok=True)
                    elif member.isfile():
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        src = tar.extractfile(member)
                        with open(target, "wb") as out:
                            shutil.copyfileobj(src, out, CHUNK)
                        if os.name != "nt" and member.mode & 0o111:
                            os.chmod(target, os.stat(target).st_mode
                                     | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        files += 1
                    elif member.issym() or member.islnk():
                        links.append((rel, member))
                    # devices / fifos are never part of a source tarball; ignore.
    finally:
        prog.close()

    log(f"extracted {files:,} files; resolving {len(links):,} links")
    materialize_links(base, links)
    (staging / VERSION_MARKER).write_text(version + "\n", encoding="utf-8")

    if dest.exists():
        log(f"replacing existing {dest}")
        shutil.rmtree(fs_path(dest))
    os.replace(base, fs_path(dest))


def installed_version(dest: Path) -> str | None:
    marker = dest / VERSION_MARKER
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Fetch upstream Chromium source for Project Mu.")
    ap.add_argument("--version", help="exact Chromium version (default: latest from Dash)")
    ap.add_argument("--channel", default="Stable",
                    choices=["Stable", "Beta", "Dev", "Canary", "Extended"])
    ap.add_argument("--platform", default="Windows",
                    choices=["Windows", "Linux", "Mac", "Android", "iOS", "ChromeOS"])
    ap.add_argument("--workdir", type=Path,
                    default=Path(os.environ.get("MU_WORKDIR", REPO_ROOT)),
                    help="where downloads/ and src/ live (env: MU_WORKDIR; default: repo root)")
    ap.add_argument("--keep-archive", action="store_true",
                    help="keep the .tar.xz after a successful unpack")
    ap.add_argument("--force", action="store_true",
                    help="re-extract even if src/ already holds this version")
    ap.add_argument("--allow-onedrive", action="store_true",
                    help="permit a workdir inside OneDrive (it will try to sync ~30 GB)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve version and probe URLs, but download nothing")
    ap.add_argument("--print-version", action="store_true",
                    help="print only the resolved version and exit (for CI)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workdir: Path = args.workdir.resolve()
    src_dir = workdir / "src"
    downloads = workdir / "downloads"

    if args.version:
        if not VERSION_RE.match(args.version):
            raise FetchError(f"--version must look like 156.0.8078.12, got {args.version!r}")
        version = args.version
        if not args.print_version:
            log(f"using pinned version {version}")
    else:
        version = resolve_version(args.channel, args.platform)
        if not args.print_version:
            log(f"latest {args.channel} for {args.platform}: {version}")

    if args.print_version:
        print(version)
        return 0

    url = TARBALL_URL.format(version=version)
    try:
        size = remote_size(url)
    except FetchError as e:
        if "HTTP 404" in str(e):
            raise FetchError(f"no official source tarball is published for {version}") from e
        raise
    sha256 = expected_sha256(url)
    log(f"tarball {url} ({fmt_bytes(size)})")
    if sha256:
        log(f"expected sha256 {sha256}")

    if args.dry_run:
        log("dry run: nothing downloaded")
        return 0

    if not args.force and installed_version(src_dir) == version:
        log(f"{src_dir} already contains Chromium {version}; nothing to do (use --force)")
        return 0

    if inside_onedrive(workdir) and not args.allow_onedrive:
        raise FetchError(
            f"workdir {workdir} is inside OneDrive, which would try to sync ~30 GB of "
            "source. Pass --workdir (or set MU_WORKDIR) to a non-synced folder such as "
            "C:\\mu-build, or use --allow-onedrive to override.")

    workdir.mkdir(parents=True, exist_ok=True)
    need = size * (UNPACK_RATIO + 1)
    free = shutil.disk_usage(workdir).free
    if free < need:
        raise FetchError(f"not enough disk space in {workdir}: "
                         f"{fmt_bytes(free)} free, ~{fmt_bytes(need)} needed")

    archive = downloads / f"chromium-{version}.tar.xz"
    download(url, archive, size, sha256)
    extract(archive, src_dir, version)

    if not args.keep_archive:
        archive.unlink()
        log("archive removed (use --keep-archive to keep it)")
    log(f"done: Chromium {version} ready in {src_dir}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    try:
        sys.exit(main())
    except FetchError as e:
        log(f"error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("interrupted - rerun to resume the download")
        sys.exit(130)
