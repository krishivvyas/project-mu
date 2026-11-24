#!/usr/bin/env python3
"""Project Mu (無) — build driver for Chromium (Windows, and Android on Linux).

Subcommands (run in this order on a fresh machine):

    pick-workdir                 print a build folder on the emptiest fixed drive
    sync     --version V         shallow gclient checkout of Chromium tag V
    doctor   [--install-sdk]     verify VS / Windows SDK / Debuggers / tools
    gen      [--args FILE]       copy args.gn into out/Default and run `gn gen`
    compile  [--deadline EPOCH]  run ninja; stop cleanly at the deadline
    package  --version V         zip the browser (and build the Inno Setup installer)
    package-android              sign ChromePublic.apk with the release key
    pack-state / unpack-state    carry a half-finished build between CI jobs

All subcommands take --workdir (env: MU_WORKDIR). The Chromium tree lives in
<workdir>/src and depot_tools must be on PATH for sync/gen. Windows builds run
on Windows; Android builds (`sync --target-os android`, `gen --args
args.android.gn`) run on Linux.
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
import struct
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
# Linux uses tar + zstd: it keeps symlinks and nanosecond mtimes, and can be
# restored as a stream so the archive and the tree never both fill the disk.
LINUX_STATE_NAME = "mu-state.tar.zst"
ANDROID_APK = OUT_REL / "apks" / "ChromePublic.apk"
BRANDING_DIR = REPO_ROOT / "branding" / "src"
INSTALLER_DIR = REPO_ROOT / "installer"
INSTALLER_ISS = INSTALLER_DIR / "project-mu.iss"
# Copied into each new profile on first run: Mu's theme and privacy settings.
INITIAL_PREFS = INSTALLER_DIR / "initial_preferences"

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
target_os = ["{target_os}"]
target_os_only = {target_os_only}
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

    # Android builds also need the Linux host tools (sysroot, clang), so only
    # Windows restricts the checkout to the target OS.
    gclient_file = GCLIENT_TEMPLATE.format(url=CHROMIUM_GIT, target_os=args.target_os,
                                           target_os_only=args.target_os == "win")
    (workdir / ".gclient").write_text(gclient_file, encoding="utf-8", newline="\n")
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

    if is_windows():
        log("ok   running on Windows (Windows build)")
    else:
        report(sys.platform.startswith("linux"), "running on Linux (Android build)",
               "Windows builds need Windows; Android builds need Linux")
    report(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}", "use Python 3.9+")
    report(bool(find_tool("gclient")), "depot_tools on PATH", "clone depot_tools and prepend it to PATH")
    if is_windows():
        report(os.environ.get("DEPOT_TOOLS_WIN_TOOLCHAIN") == "0",
               "DEPOT_TOOLS_WIN_TOOLCHAIN=0", "set it (build.py sets it for its own commands)")

    have_src = (src / "BUILD.gn").is_file()
    report(have_src, f"Chromium checkout at {src}", "run `build.py sync`")
    if not is_windows() and have_src:
        ninja = src / "third_party" / "ninja" / "ninja"
        report(ninja.is_file(), f"ninja at {ninja}",
               "Chromium may have dropped ninja; set use_siso=true in args.android.gn")
        report((src / "third_party" / "android_sdk" / "public").is_dir(),
               "Android SDK in src/third_party/android_sdk",
               "sync with `--target-os android`")
    if is_windows():
        vs = vswhere()
        wanted = supported_vs(src) if have_src else {}
        if vs:
            major = vs[1].split(".")[0]
            ok = not wanted or any(v.split(".")[0] == major for v in wanted.values())
            report(ok, f"Visual Studio {vs[1]} at {vs[0]}",
                   f"Chromium supports VS {', '.join(sorted(wanted))}")
            # base/ includes <atldef.h>; without ATL the build fails ~2,000
            # steps in with "'atldef.h' file not found".
            has_atl = bool(glob.glob(os.path.join(
                glob.escape(vs[0]), "VC", "Tools", "MSVC", "*", "atlmfc", "include", "atldef.h")))
            report(has_atl, "C++ ATL/MFC in that Visual Studio",
                   "Visual Studio Installer > Modify > Individual components > "
                   "'C++ ATL for latest build tools (x86 & x64)' and the MFC equivalent")
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
    # A resumed build (CI stage 2+) already holds the checkout and part of the
    # objects on disk, so it only needs room for what is left to compile.
    resuming = (src / OUT_REL / "build.ninja").is_file()
    need_gib = 25 if resuming else 60
    report(free > need_gib * 2**30, f"{free / 2**30:.0f} GiB free at {workdir} (need {need_gib})",
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

def apply_branding(src: Path) -> None:
    """Copy branding/src/** over the checkout (Mu's icons, logos, BRANDING).

    Only files whose bytes differ are written, so an unchanged overlay never
    bumps mtimes and never triggers rebuilds. Written files get a fresh mtime
    on purpose: ninja must see them as newer than what was built from them.
    """
    changed = 0
    for path in sorted(BRANDING_DIR.rglob("*")):
        if not path.is_file():
            continue
        dest = src / path.relative_to(BRANDING_DIR)
        data = path.read_bytes()
        if dest.is_file() and dest.read_bytes() == data:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        changed += 1
    log(f"branding: {changed} file(s) updated from {BRANDING_DIR.relative_to(REPO_ROOT)}")


PRODUCT_STRINGS = Path("chrome") / "app" / "chromium_strings.grd"
# "Chromium" as the product name; not ChromiumOS, the Chromium Authors or URLs.
CHROMIUM_WORD = re.compile(r"\bChromium\b(?!\s?OS\b| Authors\b|\.org)")
GRD_MESSAGE = re.compile(r"(<message\b[^>]*>)(.*?)(</message>)", re.S)


def rebrand_strings(src: Path) -> None:
    """Call the product "Project Mu" in the interface (window titles, menus,
    About, dialogs). Rewrites only the text of <message> elements in the
    English product strings, so attributes and descriptions are untouched; a
    regex step instead of a patch, so it survives upstream string changes.
    Other languages fall back to English for the changed strings (the file
    sets fallback_to_english). Idempotent: an already renamed file is left
    as is, mtime included."""
    path = src / PRODUCT_STRINGS
    text = path.read_bytes().decode("utf-8")  # keep line endings as they are
    count = 0

    def rename(m: re.Match) -> str:
        nonlocal count
        body, n = CHROMIUM_WORD.subn("Project Mu", m.group(2))
        count += n
        return m.group(1) + body + m.group(3)

    renamed = GRD_MESSAGE.sub(rename, text)
    if count:
        path.write_bytes(renamed.encode("utf-8"))
    log(f"product strings: {count} 'Chromium' renamed to 'Project Mu'")


def cmd_gen(args: argparse.Namespace) -> int:
    src = require_src(args.workdir)
    apply_branding(src)
    rebrand_strings(src)
    out = src / OUT_REL
    out.mkdir(parents=True, exist_ok=True)
    wanted = args.args.read_text(encoding="utf-8")
    target = out / "args.gn"
    # Rewriting identical args would bump the mtime and force a full regen.
    if not target.exists() or target.read_text(encoding="utf-8") != wanted:
        target.write_text(wanted, encoding="utf-8", newline="\n")
        log(f"wrote {target} from {args.args.name}")
    gn = src / "buildtools" / ("win/gn.exe" if is_windows() else "linux64/gn")
    gn_cmd = str(gn) if gn.is_file() else (find_tool("gn") or "")
    if not gn_cmd:
        raise BuildError(f"gn not found (expected {gn} from gclient sync)")
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
    with tempfile.TemporaryDirectory(prefix="mu-pkg-") as staging:
        # Published path -> file on disk. Mu's branding and first-run settings
        # are applied here, after compiling, so they need no Chromium rebuild.
        sources = {rel: out / rel for rel in chosen}
        for binary, groups in BRANDED_ICONS.items():
            sources[binary] = brand_icons(out / binary, Path(staging), groups)
        sources["initial_preferences"] = INITIAL_PREFS
        sources["LICENSE-project-mu.txt"] = REPO_ROOT / "LICENSE"
        # Mu's look: the Sumi theme and the New Tab page (patch 0007 loads them).
        for folder, published in (("mu-theme", "mu/theme"), ("mu-newtab", "mu/newtab")):
            base = INSTALLER_DIR / folder
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    sources[f"{published}/{f.relative_to(base).as_posix()}"] = f
        sources["Project Mu.cmd"] = INSTALLER_DIR / "Project Mu.cmd"
        # Optional content blocker (the installer asks; the zip always has it).
        ublock = fetch_ublock(args.workdir, Path(staging))
        for f in sorted(ublock.rglob("*")):
            if f.is_file():
                sources[f"{UBLOCK_DEST}/{f.relative_to(ublock).as_posix()}"] = f

        tmp = dest.with_name(dest.name + ".tmp")
        total = 0
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for rel, path in sources.items():
                z.write(path, f"{root}/{rel}")
                total += path.stat().st_size
            z.writestr(f"{root}/VERSION", args.version + "\n")
        os.replace(tmp, dest)
        digest = write_sha256(dest)
        log(f"packaged {len(sources)} files ({total / 2**20:.0f} MiB raw) into {dest} "
            f"({dest.stat().st_size / 2**20:.0f} MiB)")
        log(f"sha256 {digest}")

        if args.installer_output:
            build_installer(sources, args.version, args.installer_output.resolve())
    return 0


# --------------------------------------------------------------------------
# uBlock Origin Lite (optional, chosen in the installer)
# --------------------------------------------------------------------------

# Pinned release: bump the version and sha256 together (the digest is the one
# GitHub lists for the asset). Classic uBlock Origin is Manifest V2, which
# Chromium no longer runs; uBO Lite is its Manifest V3 edition by the same
# author. GPLv3: shipped unmodified, with its LICENSE.txt and source link.
UBLOCK_VERSION = "2026.1122.1948"
UBLOCK_SHA256 = "7cd72ae78fe6fa007193de633d2dec085a0da50f53283e821918a77978d105fb"
UBLOCK_URL = ("https://github.com/uBlockOrigin/uBOL-home/releases/download/"
              f"{UBLOCK_VERSION}/uBOLite_{UBLOCK_VERSION}.chromium.zip")
UBLOCK_DEST = "mu/ublock"  # patch 0007 loads it from here when present


def fetch_ublock(workdir: Path, staging: Path) -> Path:
    """Download (cached in the workdir), verify and unpack uBO Lite."""
    cache = workdir / "downloads" / f"uBOLite_{UBLOCK_VERSION}.chromium.zip"
    if not cache.is_file() or sha256_file(cache) != UBLOCK_SHA256:
        cache.parent.mkdir(parents=True, exist_ok=True)
        log(f"downloading uBlock Origin Lite {UBLOCK_VERSION}")
        part = cache.with_name(cache.name + ".part")
        with urllib.request.urlopen(UBLOCK_URL, timeout=120) as resp, open(part, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        got = sha256_file(part)
        if got != UBLOCK_SHA256:
            part.unlink()
            raise BuildError(f"uBlock Origin Lite download has sha256 {got}, expected {UBLOCK_SHA256}")
        os.replace(part, cache)
    dest = staging / "ublock"
    with zipfile.ZipFile(cache) as z:
        for name in z.namelist():  # refuse paths that would escape the folder
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise BuildError(f"unexpected path in uBlock Origin Lite zip: {name}")
        z.extractall(dest)
    if not (dest / "manifest.json").is_file():
        raise BuildError("uBlock Origin Lite zip has no manifest.json at its root")
    return dest


# --------------------------------------------------------------------------
# Branding (Windows): Mu's icons in chrome.exe and chrome.dll
# --------------------------------------------------------------------------

# Icon groups to replace, per binary, and the Mu icon for each. The first group
# listed for a binary must exist (it is the one that matters).
BRANDED_ICONS: dict[str, dict] = {
    # chrome/app/chrome_exe.rc. IDR_MAINFRAME is the file / shortcut icon.
    "chrome.exe": {
        "IDR_MAINFRAME": "mu.ico",
        "IDR_X001_APP_LIST": "mu.ico",
        "IDR_X003_INCOGNITO": "mu-incognito.ico",
        "IDR_X006_HTML_DOC": "mu.ico",
        "IDR_X007_PDF_DOC": "mu.ico",
        "IDR_X008_ISOLATED": "mu.ico",
        "IDR_X009_OMNIBOX_EVERYWHERE": "mu.ico",
    },
    # chrome/app/chrome_dll.rc: IDR_MAINFRAME (101) is the window and taskbar
    # icon of a running browser. 102-125 are the loading-spinner frames.
    "chrome.dll": {101: "mu.ico"},
}
RT_ICON, RT_GROUP_ICON = 3, 14
LANG_EN_US = 1033


def read_ico(path: Path) -> list[tuple[bytes, bytes]]:
    """Return (GRPICONDIRENTRY prefix without nID, image data) per icon image."""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1 or count == 0:
        raise BuildError(f"{path} is not an .ico file")
    images = []
    for i in range(count):
        w, h, colors, res, planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        prefix = struct.pack("<BBBBHHI", w, h, colors, res, planes, bpp, size)
        images.append((prefix, data[offset:offset + size]))
    return images


def brand_icons(exe: Path, staging: Path, icon_groups: dict) -> Path:
    """Copy a binary into staging with Mu's icons in place of Chromium's.

    Uses the Win32 resource-update API, so it needs Windows but no extra tools.
    """
    if not is_windows():
        raise BuildError(f"branding {exe.name} needs Windows (Win32 resource API)")
    import ctypes
    from ctypes import wintypes as W

    branded = staging / exe.name
    shutil.copyfile(exe, branded)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.restype = W.HMODULE
    k32.LoadLibraryExW.argtypes = [W.LPCWSTR, W.HANDLE, W.DWORD]
    k32.FreeLibrary.argtypes = [W.HMODULE]
    name_proc = ctypes.WINFUNCTYPE(W.BOOL, W.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    k32.EnumResourceNamesW.argtypes = [W.HMODULE, ctypes.c_void_p, name_proc, ctypes.c_void_p]
    k32.BeginUpdateResourceW.restype = W.HANDLE
    k32.BeginUpdateResourceW.argtypes = [W.LPCWSTR, W.BOOL]
    k32.UpdateResourceW.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, W.WORD,
                                    ctypes.c_void_p, W.DWORD]
    k32.EndUpdateResourceW.argtypes = [W.HANDLE, W.BOOL]

    def names(module, rtype) -> list:
        found: list = []

        def cb(_m, _t, name, _p):
            found.append(name if name < 0x10000 else ctypes.wstring_at(name))
            return True
        k32.EnumResourceNamesW(module, rtype, name_proc(cb), None)
        return found

    # LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE
    module = k32.LoadLibraryExW(str(branded), None, 0x2 | 0x20)
    if not module:
        raise BuildError(f"cannot read resources of {exe} (error {ctypes.get_last_error()})")
    try:
        next_id = max(names(module, RT_ICON), default=0) + 1
        groups = set(names(module, RT_GROUP_ICON))
    finally:
        k32.FreeLibrary(module)
    missing = [g for g in icon_groups if g not in groups]
    main = next(iter(icon_groups))
    if main in missing:
        raise BuildError(f"{exe} has no {main} icon group; upstream renamed it?")

    handle = k32.BeginUpdateResourceW(str(branded), False)
    if not handle:
        raise BuildError(f"cannot update {branded} (error {ctypes.get_last_error()})")
    ok = False
    try:
        icon_ids: dict[str, list[tuple[bytes, int]]] = {}
        for group, ico_name in icon_groups.items():
            if group in missing:
                log(f"note: {exe.name} has no {group} icon; skipped")
                continue
            if ico_name not in icon_ids:  # add each .ico's images once, share them
                entries = []
                for prefix, image in read_ico(INSTALLER_DIR / ico_name):
                    buf = ctypes.create_string_buffer(image, len(image))
                    if not k32.UpdateResourceW(handle, RT_ICON, next_id, LANG_EN_US, buf, len(image)):
                        raise BuildError(f"UpdateResource(RT_ICON) failed ({ctypes.get_last_error()})")
                    entries.append((prefix, next_id))
                    next_id += 1
                icon_ids[ico_name] = entries
            entries = icon_ids[ico_name]
            grp = struct.pack("<HHH", 0, 1, len(entries)) + b"".join(
                prefix + struct.pack("<H", rid) for prefix, rid in entries)
            gname = ctypes.create_unicode_buffer(group) if isinstance(group, str) else None
            gbuf = ctypes.create_string_buffer(grp, len(grp))
            name_arg = ctypes.addressof(gname) if gname is not None else group
            if not k32.UpdateResourceW(handle, RT_GROUP_ICON, name_arg, LANG_EN_US,
                                       gbuf, len(grp)):
                raise BuildError(f"UpdateResource({group}) failed ({ctypes.get_last_error()})")
        ok = True
    finally:
        if not k32.EndUpdateResourceW(handle, not ok) and ok:
            raise BuildError(f"EndUpdateResource failed ({ctypes.get_last_error()})")
    log(f"branded {exe.name} with Mu icons ({len(icon_groups) - len(missing)} icon groups)")
    return branded


def find_iscc() -> str:
    """Locate Inno Setup's command-line compiler (env MU_ISCC overrides)."""
    candidates = [os.environ.get("MU_ISCC", ""), shutil.which("iscc") or ""]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        for major in ("7", "6"):  # the .iss works with Inno Setup 6 and 7
            if base:
                candidates.append(os.path.join(base, f"Inno Setup {major}", "ISCC.exe"))
                candidates.append(os.path.join(base, "Programs", f"Inno Setup {major}", "ISCC.exe"))
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise BuildError("Inno Setup not found (needed for --installer-output). Install it with "
                     "`winget install JRSoftware.InnoSetup` or `choco install innosetup`, "
                     "or set MU_ISCC to ISCC.exe")


def build_installer(sources: dict[str, Path], version: str, dest: Path) -> None:
    """Compile installer/project-mu.iss over the same files as the portable zip.

    Chromium's own mini_installer has no UI, installs as "Chromium" and shares
    that product's folders, so Mu ships an Inno Setup wizard instead.
    """
    iscc = find_iscc()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mu-iss-") as tmp:
        files_iss = Path(tmp) / "files.iss"
        lines = []
        for rel, path in sources.items():
            sub = str(Path(rel).parent).replace("/", "\\")
            destdir = "{app}" if sub in ("", ".") else "{app}\\" + sub
            name = Path(rel).name
            # The wizard's Extensions page decides about optional extensions.
            check = "; Check: WantUBlock" if rel.startswith(UBLOCK_DEST + "/") else ""
            lines.append(f'Source: "{path.resolve()}"; DestDir: "{destdir}"; '
                         f'DestName: "{name}"; Flags: ignoreversion{check}')
        files_iss.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        cmd = [iscc, "/Qp", f"/DMuVersion={version}", f"/DMuFiles={files_iss}",
               f"/DMuLicense={REPO_ROOT / 'LICENSE'}", f"/O{dest.parent}", f"/F{dest.stem}",
               str(INSTALLER_ISS)]
        log(f"building installer with {iscc} ({len(sources)} files; LZMA2, takes a few minutes)")
        res = subprocess.run(cmd)
        if res.returncode != 0:
            raise BuildError(f"Inno Setup compiler exited {res.returncode}")
    if dest.suffix.lower() != ".exe" or not dest.is_file():
        raise BuildError(f"installer not written to {dest} (the output name must end in .exe)")
    log(f"installer {dest} ({dest.stat().st_size / 2**20:.0f} MiB), sha256 {write_sha256(dest)}")


def write_sha256(path: Path) -> str:
    """Write <path>.sha256 in `sha256sum -c` format and return the digest."""
    digest = sha256_file(path)
    # LF even on Windows: `sha256sum -c` on Linux/macOS rejects a CRLF line.
    path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n",
                                                     encoding="utf-8", newline="\n")
    return digest


def version_key(name: str) -> tuple:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.-]", name))


def cmd_package_android(args: argparse.Namespace) -> int:
    src = require_src(args.workdir)
    apk = src / ANDROID_APK
    if not apk.is_file():
        raise BuildError(f"{apk} missing (compile the `chrome_public_apk` target)")
    tools_root = src / "third_party" / "android_sdk" / "public" / "build-tools"
    tools = sorted((d for d in tools_root.glob("*") if (d / "apksigner").is_file()),
                   key=lambda d: version_key(d.name))
    if not tools:
        raise BuildError(f"no apksigner under {tools_root}")
    apksigner = tools[-1] / "apksigner"

    # The build signs with Chromium's public debug key. Anyone holding that key
    # could ship an "update" that Android installs over Mu with all its data,
    # so a release is only ever signed with the project's own key.
    keystore = os.environ.get("MU_ANDROID_KEYSTORE", "")
    missing = [n for n in ("MU_ANDROID_KEYSTORE", "MU_ANDROID_KEY_ALIAS",
                           "MU_ANDROID_KEYSTORE_PASSWORD", "MU_ANDROID_KEY_PASSWORD")
               if not os.environ.get(n)]
    if missing:
        raise BuildError(f"release signing not configured; set {', '.join(missing)}")
    if not Path(keystore).is_file():
        raise BuildError(f"keystore {keystore} not found")

    env = dict(os.environ)
    jdk = src / "third_party" / "jdk" / "current" / "bin"
    if jdk.is_dir():
        env["PATH"] = f"{jdk}{os.pathsep}{env.get('PATH', '')}"
    dest: Path = args.output.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Passwords are read from the environment, so they never appear in the log.
    run([str(apksigner), "sign", "--ks", keystore,
         "--ks-key-alias", os.environ["MU_ANDROID_KEY_ALIAS"],
         "--ks-pass", "env:MU_ANDROID_KEYSTORE_PASSWORD",
         "--key-pass", "env:MU_ANDROID_KEY_PASSWORD",
         "--v4-signing-enabled", "false",  # no .idsig side file to publish
         "--out", str(dest), str(apk)], src, env)
    run([str(apksigner), "verify", "--print-certs", str(dest)], src, env)
    log(f"signed {dest} ({dest.stat().st_size / 2**20:.0f} MiB), sha256 {write_sha256(dest)}")
    return 0


# --------------------------------------------------------------------------
# pack-state / unpack-state
# --------------------------------------------------------------------------

def seven_zip() -> str:
    for cand in (find_tool("7z"), r"C:\Program Files\7-Zip\7z.exe"):
        if cand and Path(cand).is_file():
            return cand
    raise BuildError("7-Zip (7z) not found")


def run_bash(script: str, cwd: Path, **kwargs) -> subprocess.Popen:
    return subprocess.Popen(["bash", "-c", "set -euo pipefail; " + script], cwd=cwd, **kwargs)


def pack_state_linux(args: argparse.Namespace, dest: Path) -> None:
    # pax format keeps nanosecond mtimes, which ninja compares to resume.
    proc = run_bash(
        f'tar --format=posix --exclude=.git -cf - src | zstd -q -T0 -{max(args.level, 1)} '
        f'| split -b {args.volume.upper()} -d -a 3 - "{dest / LINUX_STATE_NAME}."',
        args.workdir)
    if proc.wait() != 0:
        free = shutil.disk_usage(args.workdir).free
        raise BuildError(f"packing state failed ({free / 2**30:.1f} GiB free at {args.workdir})")


def unpack_state_linux(args: argparse.Namespace, source: Path) -> None:
    parts = sorted(source.glob(LINUX_STATE_NAME + ".*"))
    if not parts:
        raise BuildError(f"no {LINUX_STATE_NAME}.* in {source}")
    proc = run_bash("zstd -q -d -T0 -c | tar -xf -", args.workdir, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for part in parts:
            with open(part, "rb") as f:
                shutil.copyfileobj(f, proc.stdin, 1 << 22)
            # Free each volume as soon as it is read, so the archive and the
            # restored tree never need the disk at the same time.
            if args.delete_archive:
                part.unlink()
    except BrokenPipeError:
        pass
    proc.stdin.close()
    if proc.wait() != 0:
        free = shutil.disk_usage(args.workdir).free
        raise BuildError(f"unpacking state failed ({free / 2**30:.1f} GiB free at {args.workdir})")


def cmd_pack_state(args: argparse.Namespace) -> int:
    require_src(args.workdir)
    dest: Path = args.dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if not is_windows():
        pack_state_linux(args, dest)
        parts = sorted(dest.glob(LINUX_STATE_NAME + ".*"))
        size = sum(p.stat().st_size for p in parts)
        log(f"state packed: {len(parts)} volume(s), {size / 2**30:.2f} GiB in {dest}")
        return 0
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
    args.workdir.mkdir(parents=True, exist_ok=True)
    if src_of(args.workdir).exists():
        raise BuildError(f"{src_of(args.workdir)} already exists; refusing to overwrite")
    if not is_windows():
        unpack_state_linux(args, source)
        require_src(args.workdir)
        if args.delete_archive:
            shutil.rmtree(source, ignore_errors=True)
        log(f"state restored into {src_of(args.workdir)}")
        return 0
    first = source / f"{STATE_NAME}.001"
    if not first.is_file():
        raise BuildError(f"no {first.name} in {source}")
    # Chromium has ~95 relative symlinks that climb with "..\" (node_modules/.bin,
    # perfetto headers, ...). 7-Zip 25.01+ refuses those as "dangerous" and exits 2;
    # -snld20 restores them as before. Safe here: we made this archive in stage 1.
    try:
        run([seven_zip(), "x", "-y", "-snl", "-snld20", "-bsp1", "-bso0", f"-o{args.workdir}", str(first)],
            args.workdir, dict(os.environ))
    except BuildError as e:
        free = shutil.disk_usage(args.workdir).free
        raise BuildError(f"{e} while unpacking state ({free / 2**30:.1f} GiB free at {args.workdir})")
    require_src(args.workdir)
    if args.delete_archive:
        shutil.rmtree(source)
    log(f"state restored into {src_of(args.workdir)}")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Project Mu build driver (Windows; Android on Linux).")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workdir", type=Path,
                        default=Path(os.environ.get("MU_WORKDIR", REPO_ROOT)),
                        help="folder holding .gclient and src/ (env: MU_WORKDIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pick-workdir", help="print a build folder on the emptiest fixed drive")

    p = sub.add_parser("sync", parents=[common], help="shallow gclient checkout of a release tag")
    p.add_argument("--version", required=True)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--target-os", choices=["win", "android"], default="win",
                   help="platform to check out dependencies for (android: run on Linux)")
    p.add_argument("--allow-onedrive", action="store_true",
                   help="permit a workdir inside OneDrive (it will try to sync ~100 GB)")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("doctor", parents=[common], help="check the build environment")
    p.add_argument("--install-sdk", action="store_true",
                   help="install the Windows SDK + Debuggers if missing (needs admin)")

    p = sub.add_parser("gen", parents=[common], help="write out/Default/args.gn and run gn gen")
    p.add_argument("--args", type=Path, default=ARGS_GN,
                   help="GN args file to use (default: args.gn; Android: args.android.gn)")

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
    p.add_argument("--output", type=Path, default=REPO_ROOT / "project-mu-windows-x64-portable.zip")
    p.add_argument("--installer-output", type=Path,
                   help="also build the Inno Setup installer (installer/project-mu.iss) here")

    p = sub.add_parser("package-android", parents=[common],
                       help="sign ChromePublic.apk with the release key (env: MU_ANDROID_*)")
    p.add_argument("--output", type=Path, default=REPO_ROOT / "project-mu-android-arm64.apk")

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
    "package-android": cmd_package_android, "pack-state": cmd_pack_state, "unpack-state": cmd_unpack_state,
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
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Also show the error on the run's summary page, not only in the step log.
            print(f"::error::{str(e).splitlines()[0]}", flush=True)
        sys.exit(1)
