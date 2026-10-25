"""Host profile: what the rules' `when:` selectors match against (SPEC §2). Stdlib only.

Everything that touches the system is injectable so it can be tested without a Mac.
"""
import json
import os
import platform
import shutil
import subprocess

DEBIAN_IDS = {"debian", "ubuntu", "raspbian", "linuxmint", "pop", "kali", "elementary"}
FEDORA_IDS = {"fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn"}


def read_os_release(paths=("/etc/os-release", "/usr/lib/os-release")):
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        out = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key] = val.strip().strip('"').strip("'")
        return out
    return {}


def detect_family(system, osr):
    """'debian' | 'fedora' | 'macos' | None (unsupported)."""
    if system == "Darwin":
        return "macos"
    if system != "Linux":
        return None
    ids = [osr.get("ID", "").lower()] + osr.get("ID_LIKE", "").lower().split()
    if any(i in DEBIAN_IDS for i in ids):
        return "debian"
    if any(i in FEDORA_IDS for i in ids):
        return "fedora"
    return None


def _normalize_arch(machine):
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine.lower(), machine.lower())


def _probe_dialect(tool, run):
    """'gnu' if `tool --version` identifies GNU, 'bsd' for any other working implementation."""
    try:
        res = run([tool, "--version"])
    except (OSError, subprocess.SubprocessError):
        return None
    text = (res.stdout or b"") + (res.stderr or b"")
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return "gnu" if "GNU" in text else "bsd"


def _run(argv):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)


def probe_host(rules, system=None, machine=None, osr=None, which=None, run=None,
               geteuid=None, mac_ver=None, exists=None):
    """Build the host profile for `rules`. Arguments exist for tests."""
    system = system or platform.system()
    osr = read_os_release() if osr is None else osr
    which = which or shutil.which
    run = run or _run
    exists = exists or os.path.exists
    euid = (geteuid or getattr(os, "geteuid", lambda: 1))()

    family = detect_family(system, osr)
    if system == "Darwin":
        ver = (mac_ver or (lambda: platform.mac_ver()[0]))()
        os_id = "macos-%s" % (ver.split(".")[0] if ver else "unknown")
    else:
        os_id = "%s-%s" % (osr.get("ID", "linux"), osr.get("VERSION_ID", "unknown"))

    commands = set(rules.cmds) | {"sudo"}
    host = {
        "family": family,
        "os_id": os_id,
        "arch": _normalize_arch(machine or platform.machine()),
        "has": {c: which(c) is not None for c in sorted(commands)},
        "dialect": {},
        "is_root": euid == 0,
    }
    for prefix in ("/opt/homebrew", "/usr/local", "/home/linuxbrew/.linuxbrew"):
        if exists(os.path.join(prefix, "bin", "brew")):
            host["brew_prefix"] = prefix
            break
    tools = set()
    for _intent, form in rules.forms:
        tools.update((form["when"].get("dialect") or {}).keys())
    for tool in sorted(tools):
        if host["has"].get(tool) or which(tool):
            d = _probe_dialect(tool, run)
            if d:
                host["dialect"][tool] = d
    return host


def get_host(rules, environ=None):
    """USH_HOST (JSON) overrides probing: used by tests and by people who want to pin a profile."""
    environ = os.environ if environ is None else environ
    raw = environ.get("USH_HOST")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "family" in data:
                return data
        except ValueError:
            pass
    return probe_host(rules)
