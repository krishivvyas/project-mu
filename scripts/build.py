#!/usr/bin/env python3
"""Project Mu (無) — Windows build driver for Chromium.

Subcommands (run in this order on a fresh machine):

    pick-workdir                 print a build folder on the emptiest fixed drive
    sync     --version V         shallow gclient checkout of Chromium tag V
    doctor   [--install-sdk]     verify VS / Windows SDK / Debuggers / tools
    gen                          copy args.gn into out/Default and run `gn gen`
    compile  [--deadline EPOCH]  run ninja; stop cleanly at the deadline
    package  --version V         zip the browser into project-mu-release.zip
    pack-state / unpack-state    carry a half-finished build between CI jobs

All subcommands take --workdir (env: MU_WORKDIR). The Chromium tree lives in
<workdir>/src and depot_tools must be on PATH for sync/gen.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARGS_GN = REPO_ROOT / "args.gn"
OUT_REL = Path("out") / "Default"
VERSION_MARKER = ".mu_version"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
CHROMIUM_GIT = "https://chromium.googlesource.com/chromium/src.git"
STATE_NAME = "mu-state.7z"

# Windows SDK 10.0.28000.2957 (September 2026), official Microsoft fwlink.
# Override with MU_WINSDK_URL when Chromium starts requiring a newer SDK.
WINSDK_URL = "https://go.microsoft.com/fwlink/?linkid=2382519"

# Must exist in a usable package; everything else in the archive list is optional.
REQUIRED_FILES = ["chrome.exe", "chrome.dll", "chrome_elf.dll", "resources.pak",
                  "icudtl.dat", "v8_context_snapshot.bin"]
# Used only if src/infra/archive_config/win-archive-rel.json is missing.
FALLBACK_FILES = REQUIRED_FILES + [
    "chrome_100_percent.pak", "chrome_200_percent.pak", "chrome_proxy.exe",
    "chrome_pwa_launcher.exe", "chrome_wer.dll", "D3DCompiler_47.dll", "dxcompiler.dll",
    "elevation_service.exe", "eventlog_provider.dll", "libEGL.dll", "libGLESv2.dll",
    "notification_helper.exe", "vk_swiftshader.dll", "vk_swiftshader_icd.json", "vulkan-1.dll",
]
FALLBACK_GLOBS = ["chrome_renderer.dll", "locales/*.pak", "*.manifest"]
SKIP_PATTERNS = ["*_tests.exe", "*_unittests.exe", "setup.exe"]

# Process-scoped git config (no edits to the user's ~/.gitconfig).
GIT_ENV_CONFIG = {
    "core.autocrlf": "false",
    "core.filemode": "false",
    "core.longpaths": "true",
    "core.preloadindex": "true",
    "core.fscache": "true",
}


class BuildError(RuntimeError):
    pass


def log(msg: str = "") -> None:
    print(f"[mu] {msg}" if msg else "", flush=True)


# --------------------------------------------------------------------------
# Environment helpers
# --------------------------------------------------------------------------

def is_windows() -> bool:
    return os.name == "nt"


def src_of(workdir: Path) -> Path:
    return workdir / "src"


def require_src(workdir: Path) -> Path:
    src = src_of(workdir)
    if not (src / "BUILD.gn").is_file():
        raise BuildError(f"no Chromium checkout at {src} (run `build.py sync` first)")
    return src


def find_tool(name: str) -> str | None:
    return shutil.which(name)


def vswhere() -> tuple[str, str] | None:
    """Return (installationPath, installationVersion) of the newest VS with C++."""
    exe = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
               "Microsoft Visual Studio", "Installer", "vswhere.exe")
    if not exe.is_file():
        return None
    res = subprocess.run(
        [str(exe), "-latest", "-products", "*", "-prerelease",
         "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
         "-format", "json"], capture_output=True, text=True)
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    if not data:
        return None
    return data[0]["installationPath"], data[0]["installationVersion"]


def supported_vs(src: Path) -> dict[str, str]:
    """Parse {'2026': '18.0', ...} from Chromium's build/vs_toolchain.py."""
    try:
        text = (src / "build" / "vs_toolchain.py").read_text(encoding="utf-8")
    except OSError:
        return {}
    return dict(re.findall(r"\(\s*'(\d{4})'\s*,\s*'(\d+\.\d+)'\s*\)", text))


def required_sdk(src: Path) -> str | None:
    try:
        text = (src / "build" / "vs_toolchain.py").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^SDK_VERSION\s*=\s*'([\d.]+)'", text, re.M)
    return m.group(1) if m else None


def kits_root() -> Path:
    if is_windows():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as k:
                return Path(winreg.QueryValueEx(k, "KitsRoot10")[0])
        except OSError:
            pass
    return Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                "Windows Kits", "10")


def build_env(src: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Append our git settings to any GIT_CONFIG_* the caller already set.
    count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    for key, value in GIT_ENV_CONFIG.items():
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    if is_windows() and src is not None:
        vs = vswhere()
        if vs:
            major = vs[1].split(".")[0]
            for year, ver in supported_vs(src).items():
                if ver.split(".")[0] == major:
                    env.setdefault(f"vs{year}_install", vs[0])
    return env


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    log("$ " + " ".join(cmd))
    res = subprocess.run(cmd, cwd=cwd, env=env)
    if res.returncode != 0:
        raise BuildError(f"command failed with exit code {res.returncode}: {cmd[0]}")


# --------------------------------------------------------------------------
# pick-workdir
# --------------------------------------------------------------------------

def cmd_pick_workdir(args: argparse.Namespace) -> int:
    if not is_windows():
        print(Path.home() / "mu")
        return 0
    import ctypes
    best, best_free = None, -1
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:  # DRIVE_FIXED
            continue
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        print(f"[mu] {root} {free / 2**30:.1f} GiB free", file=sys.stderr)
        if free > best_free:
            best, best_free = root, free
    if best is None:
        raise BuildError("no fixed drives found")
    print(Path(best) / "mu")
    return 0


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------

GCLIENT_TEMPLATE = """\
# Generated by Project Mu scripts/build.py - do not edit.
solutions = [
  {{
    "name": "src",
    "url": "{url}",
    "managed": False,
    "custom_deps": {{}},
    "custom_vars": {{"checkout_pgo_profiles": False}},
  }},
]
target_os = ["win"]
target_os_only = True
"""


def gclient_sync_cmd(gclient: str, version: str, jobs: int) -> list[str]:
    return [gclient, "sync",
            "--revision", f"src@refs/tags/{version}",
            "--no-history", "--delete_unversioned_trees", "--force", "--reset",
            "--jobs", str(jobs)]


def cmd_sync(args: argparse.Namespace) -> int:
    version = args.version
    if not VERSION_RE.match(version):
        raise BuildError(f"--version must look like 156.0.8078.12, got {version!r}")
    workdir: Path = args.workdir
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fetch_upstream import inside_onedrive
    if inside_onedrive(workdir) and not args.allow_onedrive:
        raise BuildError(
            f"workdir {workdir} is inside OneDrive, which would try to sync a ~100 GB "
            "Chromium checkout. Pass --workdir (or set MU_WORKDIR) to a non-synced folder "
            "such as C:\\mu-build, or use --allow-onedrive to override.")
    workdir.mkdir(parents=True, exist_ok=True)
    src = src_of(workdir)

    gclient = find_tool("gclient")
    if not gclient and not args.dry_run:
        raise BuildError("gclient not found; put depot_tools on PATH first")

    # --reset restores tracked files but leaves files our patches created, so
    # undo the Mu patch set before syncing over a previously patched tree.
    if (src / ".mu" / "applied.json").exists() and not args.dry_run:
        log("reverting Mu patches before sync")
        import apply_patches
        if apply_patches.main(["--src", str(src), "--revert"]) != 0:
            raise BuildError("could not revert Mu patches; delete src/ and sync again")

    (workdir / ".gclient").write_text(GCLIENT_TEMPLATE.format(url=CHROMIUM_GIT),
                                      encoding="utf-8", newline="\n")
    cmd = gclient_sync_cmd(gclient or "gclient", version, args.jobs)
    if args.dry_run:
        log(f"dry run: would write {workdir / '.gclient'} and run:")
        log("$ " + " ".join(cmd))
        return 0
    run(cmd, workdir, build_env())
    (src / VERSION_MARKER).write_text(version + "\n", encoding="utf-8")
    log(f"Chromium {version} synced into {src}")
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def sdk_status(src: Path) -> tuple[str | None, bool, bool]:
    need = required_sdk(src)
    root = kits_root()
    has_sdk = bool(need) and (root / "Include" / need / "um" / "windows.h").is_file() \
        and (root / "Lib" / need / "um" / "x64").is_dir()
    has_dbg = (root / "Debuggers" / "x64" / "dbghelp.dll").is_file()
    return need, has_sdk, has_dbg


def install_sdk(url: str) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mu-winsdk-"))
    exe = tmp / "winsdksetup.exe"
    log(f"downloading Windows SDK installer from {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "project-mu-build/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(exe, "wb") as f:
        shutil.copyfileobj(r, f)
    logfile = tmp / "winsdk.log"
    log("installing Windows SDK (all features, quiet) - this takes several minutes")
    res = subprocess.run([str(exe), "/features", "+", "/quiet", "/norestart",
                          "/ceip", "off", "/log", str(logfile)])
    if res.returncode not in (0, 3010):  # 3010 = success, reboot pending
        tail = logfile.read_text(errors="replace")[-3000:] if logfile.exists() else ""
        raise BuildError(f"Windows SDK installer exited {res.returncode}\n{tail}")
    log("Windows SDK installed")


def cmd_doctor(args: argparse.Namespace) -> int:
    workdir: Path = args.workdir
    src = src_of(workdir)
    problems: list[str] = []

    def report(ok: bool, what: str, fix: str = "") -> None:
        log(f"{'ok  ' if ok else 'MISS'} {what}")
        if not ok:
            problems.append(f"{what}: {fix}" if fix else what)

    report(is_windows(), "running on Windows", "Chromium for Windows must be built on Windows")
    report(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}", "use Python 3.9+")
    report(bool(find_tool("gclient")), "depot_tools on PATH", "clone depot_tools and prepend it to PATH")
    report(os.environ.get("DEPOT_TOOLS_WIN_TOOLCHAIN") == "0",
           "DEPOT_TOOLS_WIN_TOOLCHAIN=0", "set it (build.py sets it for its own commands)")

    have_src = (src / "BUILD.gn").is_file()
    report(have_src, f"Chromium checkout at {src}", "run `build.py sync`")
    if is_windows():
        vs = vswhere()
        wanted = supported_vs(src) if have_src else {}
        if vs:
            major = vs[1].split(".")[0]
            ok = not wanted or any(v.split(".")[0] == major for v in wanted.values())
            report(ok, f"Visual Studio {vs[1]} at {vs[0]}",
                   f"Chromium supports VS {', '.join(sorted(wanted))}")
        else:
            report(False, "Visual Studio with C++ workload", "install VS with Desktop C++ + ATL/MFC")

        if have_src:
            need, has_sdk, has_dbg = sdk_status(src)
            if (not has_sdk or not has_dbg) and args.install_sdk:
                install_sdk(os.environ.get("MU_WINSDK_URL", WINSDK_URL))
                need, has_sdk, has_dbg = sdk_status(src)
            report(has_sdk, f"Windows SDK {need} in {kits_root()}",
                   "rerun with --install-sdk")
            report(has_dbg, "Debugging Tools for Windows (Debuggers\\x64\\dbghelp.dll)",
                   "rerun with --install-sdk")
            ninja = src / "third_party" / "ninja" / "ninja.exe"
            report(ninja.is_file(), f"ninja at {ninja}",
                   "Chromium may have dropped ninja; set use_siso=true in args.gn")

    free = shutil.disk_usage(workdir if workdir.exists() else workdir.anchor or ".").free
    report(free > 60 * 2**30, f"{free / 2**30:.0f} GiB free at {workdir}",
           "Chromium needs roughly 100 GiB for checkout + build")

    if problems:
        log()
        log(f"{len(problems)} problem(s):")
        for p in problems:
            log(f"  - {p}")
        return 1
    log("environment ready")
    return 0


# --------------------------------------------------------------------------
# gen
# --------------------------------------------------------------------------

def cmd_gen(args: argparse.Namespace) -> int:
    src = require_src(args.workdir)
    out = src / OUT_REL
    out.mkdir(parents=True, exist_ok=True)
    wanted = ARGS_GN.read_text(encoding="utf-8")
    target = out / "args.gn"
    # Rewriting identical args would bump the mtime and force a full regen.
    if not target.exists() or target.read_text(encoding="utf-8") != wanted:
        target.write_text(wanted, encoding="utf-8", newline="\n")
        log(f"wrote {target}")
    gn = src / "buildtools" / "win" / "gn.exe"
    gn_cmd = str(gn) if gn.is_file() else (find_tool("gn") or "")
    if not gn_cmd:
        raise BuildError("gn not found (expected src/buildtools/win/gn.exe from gclient sync)")
    run([gn_cmd, "gen", str(OUT_REL)], src, build_env(src))
    return 0


# --------------------------------------------------------------------------
# compile
# --------------------------------------------------------------------------

def interrupt(proc: subprocess.Popen, grace: float) -> None:
    """Ask the build to stop like Ctrl-C would, then force-kill the tree."""
    try:
        if is_windows():
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)
    except OSError:
        pass
    try:
        proc.wait(grace)
        return
    except subprocess.TimeoutExpired:
        log(f"build did not stop within {grace:.0f}s; killing process tree")
    if is_windows():
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def write_github_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def cmd_compile(args: argparse.Namespace) -> int:
    src = require_src(args.workdir)
    if not (src / OUT_REL / "build.ninja").is_file():
        raise BuildError("out/Default is not generated (run `build.py gen`)")
    ninja = args.ninja or str(src / "third_party" / "ninja" / ("ninja.exe" if is_windows() else "ninja"))
    jobs = args.jobs or (os.cpu_count() or 2)
    cmd = [*([sys.executable] if ninja.endswith(".py") else []), ninja,
           "-C", str(OUT_REL), "-j", str(jobs), *args.targets]

    deadline = args.deadline
    if args.budget_minutes is not None:
        deadline = time.time() + args.budget_minutes * 60
    if deadline is not None:
        remaining = deadline - time.time()
        log(f"time budget: {remaining / 60:.0f} min")
        if remaining < 60:
            log("less than a minute of budget left; not starting the build")
            write_github_output(done="false")
            return 0

    log("$ " + " ".join(cmd))
    kwargs: dict = {"cwd": src, "env": build_env(src)}
    if is_windows():
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    timed_out = False
    try:
        if deadline is None:
            proc.wait()
        else:
            try:
                proc.wait(max(deadline - time.time(), 0))
            except subprocess.TimeoutExpired:
                timed_out = True
                log("time budget reached; stopping the build so the next stage can resume")
                interrupt(proc, args.grace)
    except KeyboardInterrupt:
        interrupt(proc, args.grace)
        raise

    if proc.returncode == 0:
        log("build complete")
        write_github_output(done="true")
        return 0
    if timed_out:
        write_github_output(done="false")
        return 0
    raise BuildError(f"ninja failed with exit code {proc.returncode}")


# --------------------------------------------------------------------------
# package
# --------------------------------------------------------------------------

def archive_manifest(src: Path) -> tuple[list[str], list[str]]:
    cfg = src / "infra" / "archive_config" / "win-archive-rel.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        for entry in data["archive_datas"]:
            if entry.get("gcs_path", "").endswith("/chrome-win.zip"):
                norm = lambda xs: [x.replace("\\", "/") for x in xs]  # noqa: E731
                return norm(entry.get("files", [])), norm(entry.get("file_globs", []))
        log(f"warning: no chrome-win.zip entry in {cfg}; using built-in file list")
    except (OSError, ValueError, KeyError) as e:
        log(f"warning: cannot read {cfg} ({e}); using built-in file list")
    return list(FALLBACK_FILES), list(FALLBACK_GLOBS)


def collect_files(out: Path, files: list[str], globs: list[str]) -> list[str]:
    def skipped(rel: str) -> bool:
        return any(fnmatch.fnmatch(Path(rel).name.lower(), p) for p in SKIP_PATTERNS)

    chosen: list[str] = []
    for rel in files:
        if skipped(rel):
            continue
        if (out / rel).is_file():
            chosen.append(rel)
        elif rel in REQUIRED_FILES:
            raise BuildError(f"required file missing from build output: {rel}")
        else:
            log(f"note: {rel} not built by these targets; skipped")
    for pattern in globs:
        for match in sorted(glob.glob(os.path.join(glob.escape(str(out)), pattern))):
            rel = Path(match).relative_to(out).as_posix()
            if Path(match).is_file() and not skipped(rel) and rel not in chosen:
                chosen.append(rel)
    missing = [r for r in REQUIRED_FILES if r not in chosen]
    if missing:
        raise BuildError(f"required files missing from build output: {', '.join(missing)}")
    return chosen


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def cmd_package(args: argparse.Namespace) -> int:
    src = require_src(args.workdir)
    out = src / OUT_REL
    files, globs = archive_manifest(src)
    chosen = collect_files(out, files, globs)
    root = f"project-mu-{args.version}"
    dest: Path = args.output.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    total = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel in chosen:
            z.write(out / rel, f"{root}/{rel}")
            total += (out / rel).stat().st_size
        z.write(REPO_ROOT / "LICENSE", f"{root}/LICENSE-project-mu.txt")
        z.writestr(f"{root}/VERSION", args.version + "\n")
    os.replace(tmp, dest)
    digest = sha256_file(dest)
    dest.with_name(dest.name + ".sha256").write_text(f"{digest}  {dest.name}\n", encoding="utf-8")
    log(f"packaged {len(chosen)} files ({total / 2**20:.0f} MiB raw) into {dest} "
        f"({dest.stat().st_size / 2**20:.0f} MiB)")
    log(f"sha256 {digest}")
    return 0


# --------------------------------------------------------------------------
# pack-state / unpack-state
# --------------------------------------------------------------------------

def seven_zip() -> str:
    for cand in (find_tool("7z"), r"C:\Program Files\7-Zip\7z.exe"):
        if cand and Path(cand).is_file():
            return cand
    raise BuildError("7-Zip (7z) not found")


def cmd_pack_state(args: argparse.Namespace) -> int:
    require_src(args.workdir)
    dest: Path = args.dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    # .git dirs are only needed for syncing; the build never reads them.
    cmd = [seven_zip(), "a", "-t7z", f"-mx={args.level}", "-mmt=on", "-snl",
           f"-v{args.volume}", "-xr!.git", "-bsp1", "-bso0",
           str(dest / STATE_NAME), "src"]
    run(cmd, args.workdir, dict(os.environ))
    parts = sorted(dest.glob(STATE_NAME + ".*"))
    size = sum(p.stat().st_size for p in parts)
    log(f"state packed: {len(parts)} volume(s), {size / 2**30:.2f} GiB in {dest}")
    return 0


def cmd_unpack_state(args: argparse.Namespace) -> int:
    source: Path = args.source.resolve()
    first = source / f"{STATE_NAME}.001"
    if not first.is_file():
        raise BuildError(f"no {first.name} in {source}")
    args.workdir.mkdir(parents=True, exist_ok=True)
    if src_of(args.workdir).exists():
        raise BuildError(f"{src_of(args.workdir)} already exists; refusing to overwrite")
    run([seven_zip(), "x", "-y", "-snl", "-bsp1", "-bso0", f"-o{args.workdir}", str(first)],
        args.workdir, dict(os.environ))
    require_src(args.workdir)
    if args.delete_archive:
        shutil.rmtree(source)
    log(f"state restored into {src_of(args.workdir)}")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Project Mu Windows build driver.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workdir", type=Path,
                        default=Path(os.environ.get("MU_WORKDIR", REPO_ROOT)),
                        help="folder holding .gclient and src/ (env: MU_WORKDIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pick-workdir", help="print a build folder on the emptiest fixed drive")

    p = sub.add_parser("sync", parents=[common], help="shallow gclient checkout of a release tag")
    p.add_argument("--version", required=True)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--allow-onedrive", action="store_true",
                   help="permit a workdir inside OneDrive (it will try to sync ~100 GB)")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("doctor", parents=[common], help="check the build environment")
    p.add_argument("--install-sdk", action="store_true",
                   help="install the Windows SDK + Debuggers if missing (needs admin)")

    sub.add_parser("gen", parents=[common], help="write out/Default/args.gn and run gn gen")

    p = sub.add_parser("compile", parents=[common], help="build with ninja")
    p.add_argument("targets", nargs="*", default=["chrome"])
    when = p.add_mutually_exclusive_group()
    when.add_argument("--deadline", type=float, help="unix time at which to stop")
    when.add_argument("--budget-minutes", type=float, help="minutes from now at which to stop")
    p.add_argument("--jobs", type=int, help="parallel jobs (default: CPU count)")
    p.add_argument("--grace", type=float, default=120.0, help=argparse.SUPPRESS)
    p.add_argument("--ninja", help=argparse.SUPPRESS)

    p = sub.add_parser("package", parents=[common], help="zip the built browser")
    p.add_argument("--version", required=True)
    p.add_argument("--output", type=Path, default=REPO_ROOT / "project-mu-release.zip")

    p = sub.add_parser("pack-state", parents=[common], help="archive src/ for the next CI job")
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--volume", default="2g", help="7z volume size (default 2g)")
    p.add_argument("--level", type=int, default=1, choices=range(0, 10))

    p = sub.add_parser("unpack-state", parents=[common], help="restore src/ from pack-state")
    p.add_argument("--from", dest="source", type=Path, required=True)
    p.add_argument("--delete-archive", action="store_true")

    args = ap.parse_args(argv)
    if hasattr(args, "workdir"):
        args.workdir = args.workdir.resolve()
    return args


COMMANDS = {
    "pick-workdir": cmd_pick_workdir, "sync": cmd_sync, "doctor": cmd_doctor,
    "gen": cmd_gen, "compile": cmd_compile, "package": cmd_package,
    "pack-state": cmd_pack_state, "unpack-state": cmd_unpack_state,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return COMMANDS[args.cmd](args)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    try:
        sys.exit(main())
    except BuildError as e:
        log(f"error: {e}")
        sys.exit(1)
