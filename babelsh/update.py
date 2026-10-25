"""`ush update`: fetch, verify and atomically install a signed rules release (SPEC §6).

Trust model: the only things trusted before verification are the pinned public keys. Every
byte of the manifest is checked against a minisign signature from each required role BEFORE
it is parsed; every other file is checked against the signed manifest's size and SHA-256.
A release is installed only if its version is higher than anything installed before
(`high_water`), so a replayed old release cannot roll the client back.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request

from . import minisign
from ._version import __version__
from .rules import Rules, RulesError

MAX_MANIFEST = 256 * 1024
MAX_SIGNATURE = 8 * 1024
MAX_FILE = 16 * 1024 * 1024
SUPPORTED_RULES_SCHEMA = 1
KEEP_RELEASES = 3

BUNDLED_TRUSTED_KEYS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                                    "trusted_keys.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_STAGE_RANK = {"a": 0, "b": 1, "rc": 2, None: 3}
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------------------
# paths / state
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def config_dir(environ):
    return os.path.join(environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "ush")


def _read_int(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return 0


def installed_manifest(state):
    try:
        with open(os.path.join(state, "current", "manifest.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def version_floor(state):
    """Highest version ever installed. Releases not newer than this are never accepted."""
    cur = installed_manifest(state)
    cur_version = cur.get("version", 0) if isinstance(cur, dict) else 0
    return max(_read_int(os.path.join(state, "high_water")), cur_version if isinstance(cur_version, int) else 0)


def load_trusted_keys(environ):
    path = environ.get("USH_TRUSTED_KEYS") or BUNDLED_TRUSTED_KEYS
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise UpdateError("cannot read trusted keys (%s): %s" % (path, exc))
    if not isinstance(data, dict):
        raise UpdateError("trusted keys file must be an object {role: [keys]}")
    out = {}
    for role, keys in data.items():
        if not _ROLE.match(role) or not isinstance(keys, list) or \
                not all(isinstance(k, str) for k in keys):
            raise UpdateError("trusted keys: bad entry for role %r" % role)
        if keys:
            out[role] = keys
    return out


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

class _NoDowngrade(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(req.full_url).scheme == "https" and \
                urllib.parse.urlparse(newurl).scheme != "https":
            raise UpdateError("refusing redirect from https to %s" % newurl)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


def fetch_url(url, cap, timeout=15):
    if urllib.parse.urlparse(url).scheme not in ("http", "https", "file"):
        raise UpdateError("unsupported URL scheme in %s (use https://, http:// or file://)" % url)
    try:
        with urllib.request.build_opener(_NoDowngrade).open(url, timeout=timeout) as resp:
            data = resp.read(cap + 1)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError("cannot fetch %s: %s" % (url, getattr(exc, "reason", exc)))
    if len(data) > cap:
        raise UpdateError("%s is larger than the allowed %d bytes" % (url, cap))
    return data


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def _semver(text):
    """Sortable key for 'X.Y.Z' with an optional a/b/rc pre-release suffix (0.1.0a1 < 0.1.0)."""
    m = _SEMVER.match(text)
    if not m:
        raise UpdateError("unsupported version string %r" % (text,))
    major, minor, patch, stage, num = m.groups()
    return (int(major), int(minor), int(patch), _STAGE_RANK[stage], int(num or 0))


def _ts(value, field):
    try:
        return datetime.datetime.strptime(value, _TS_FORMAT)
    except (TypeError, ValueError):
        raise UpdateError("manifest %s must look like 2026-10-26T09:00:00Z" % field)


def validate_manifest(m):
    """Check a (already signature-verified) manifest; return it with parsed timestamps."""
    if not isinstance(m, dict):
        raise UpdateError("manifest must be an object")
    allowed = {"format", "rules_schema", "version", "released_at", "expires", "min_client",
               "source_commit", "signing", "files", "stats", "key_rotation"}
    extra = set(m) - allowed
    if extra:
        raise UpdateError("manifest has unknown fields %s" % sorted(extra))
    if m.get("format") != "ush-rules-release":
        raise UpdateError("not a ush rules manifest")
    for field in ("rules_schema", "version"):
        if not isinstance(m.get(field), int) or isinstance(m.get(field), bool) or m[field] < 1:
            raise UpdateError("manifest %s must be a positive integer" % field)
    if not isinstance(m.get("min_client"), str) or not _SEMVER.match(m["min_client"]):
        raise UpdateError("manifest min_client must be a version like 0.1.0 or 0.1.0a1")
    m["_released"] = _ts(m.get("released_at"), "released_at")
    m["_expires"] = _ts(m.get("expires"), "expires")
    signing = m.get("signing")
    if not (isinstance(signing, dict) and signing.get("scheme") == "minisign"
            and isinstance(signing.get("roles"), list)
            and all(isinstance(r, str) and _ROLE.match(r) for r in signing["roles"])):
        raise UpdateError("manifest signing block must be {scheme: minisign, roles: [...]}")
    files = m.get("files")
    if not isinstance(files, list) or not files:
        raise UpdateError("manifest lists no files")
    seen = set()
    for f in files:
        if not (isinstance(f, dict) and set(f) == {"path", "sha256", "size"}
                and isinstance(f["path"], str) and _FILENAME.match(f["path"])
                and isinstance(f["sha256"], str) and _SHA256.match(f["sha256"])
                and isinstance(f["size"], int) and not isinstance(f["size"], bool)
                and 0 < f["size"] <= MAX_FILE):
            raise UpdateError("manifest has a malformed file entry: %r" % (f,))
        if f["path"] in seen or f["path"].startswith("manifest.json"):
            raise UpdateError("manifest file list has a duplicate or reserved name: %s" % f["path"])
        seen.add(f["path"])
    if "rules.json" not in seen:
        raise UpdateError("manifest does not list rules.json")
    return m


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def diff_rules(old, new, limit=40):
    """Human-readable summary of what a release changes. `old` may be None."""
    old = old or {}
    lines = []

    def section(label, o, n):
        for k in sorted(set(n) - set(o)):
            lines.append("+ %s %s" % (label, k))
        for k in sorted(set(o) - set(n)):
            lines.append("- %s %s" % (label, k))
        for k in sorted(k for k in set(o) & set(n) if o[k] != n[k]):
            lines.append("~ %s %s" % (label, k))

    section("package", old.get("concepts", {}), new.get("concepts", {}))
    section("service", old.get("services", {}), new.get("services", {}))
    section("intent", {i["id"]: i for i in old.get("intents", [])},
            {i["id"]: i for i in new.get("intents", [])})
    if len(lines) > limit:
        lines = lines[:limit] + ["... and %d more" % (len(lines) - limit)]
    return lines


# ---------------------------------------------------------------------------
# install / rollback
# ---------------------------------------------------------------------------

def _atomic_symlink(target, link):
    tmp = "%s.new.%d" % (link, os.getpid())
    try:
        os.unlink(tmp)
    except OSError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, link)


def _atomic_write(path, text):
    tmp = "%s.new.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _link_target(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def install_release(state, version, files):
    """Write the release next to the others, then flip `current` in one atomic step."""
    releases = os.path.join(state, "releases")
    os.makedirs(releases, mode=0o700, exist_ok=True)
    final = os.path.join(releases, str(version))
    staging = final + ".partial"
    shutil.rmtree(staging, ignore_errors=True)
    os.mkdir(staging, 0o700)
    try:
        for name, data in files.items():
            with open(os.path.join(staging, name), "wb") as fh:
                fh.write(data)
        if os.path.exists(final):
            raise UpdateError("release %s is already present on disk" % version)
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    old = _link_target(os.path.join(state, "current"))
    if old:
        _atomic_symlink(old, os.path.join(state, "previous"))
    _atomic_symlink(os.path.join("releases", str(version)), os.path.join(state, "current"))
    _atomic_write(os.path.join(state, "high_water"), "%d\n" % max(version, _read_int(
        os.path.join(state, "high_water"))))
    _prune(state)


def _prune(state):
    releases = os.path.join(state, "releases")
    keep = set()
    for link in ("current", "previous"):
        target = _link_target(os.path.join(state, link))
        if target:
            keep.add(os.path.basename(target))
    names = sorted((n for n in os.listdir(releases) if n.isdigit()), key=int, reverse=True)
    keep.update(names[:KEEP_RELEASES])
    for n in names:
        if n not in keep:
            shutil.rmtree(os.path.join(releases, n), ignore_errors=True)


def rollback(state):
    """Swap `current` and `previous`. The version floor is unchanged, so a rolled-back client
    still refuses to 'update' to anything it has already seen."""
    cur = _link_target(os.path.join(state, "current"))
    prev = _link_target(os.path.join(state, "previous"))
    if not prev or not os.path.isdir(os.path.join(state, prev)):
        raise UpdateError("no previous release to roll back to")
    _atomic_symlink(prev, os.path.join(state, "current"))
    if cur:
        _atomic_symlink(cur, os.path.join(state, "previous"))
    return os.path.basename(prev)


def status(state, now=None):
    now = now or _utcnow()
    m = installed_manifest(state)
    out = {"state_dir": state, "version_floor": version_floor(state), "installed": None}
    if isinstance(m, dict):
        stale = False
        try:
            stale = now > _ts(m.get("expires"), "expires")
        except UpdateError:
            pass
        out["installed"] = {"version": m.get("version"), "released_at": m.get("released_at"),
                            "expires": m.get("expires"), "stale": stale,
                            "previous": _link_target(os.path.join(state, "previous"))}
    return out


# ---------------------------------------------------------------------------
# the update itself
# ---------------------------------------------------------------------------

class Result(object):
    def __init__(self, status, version, installed_version, diff=(), warnings=()):
        self.status = status                # installed | dry_run | up_to_date
        self.version = version              # release offered by the server
        self.installed_version = installed_version
        self.diff = list(diff)
        self.warnings = list(warnings)


def _current_rules_data(state):
    for path in (os.path.join(state, "current", "rules.json"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rules.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return None


def update(base_url, environ, now=None, dry_run=False, fetch=fetch_url,
           client_version=__version__):
    now = now or _utcnow()
    state = config_dir(environ)
    trusted = load_trusted_keys(environ)
    if not trusted:
        raise UpdateError("no trusted keys are configured, so no update can be verified. Pin the "
                          "publisher's minisign public key in %s (or set USH_TRUSTED_KEYS)"
                          % BUNDLED_TRUSTED_KEYS)
    base = base_url.rstrip("/")
    warnings = []

    # 1. manifest bytes + one signature per required role; verify before parsing anything.
    manifest_raw = fetch(base + "/manifest.json", MAX_MANIFEST)
    sigs = {}
    for role in sorted(trusted):
        sig_raw = fetch("%s/manifest.json.%s.minisig" % (base, role), MAX_SIGNATURE)
        try:
            minisign.verify(manifest_raw, sig_raw.decode("utf-8"), trusted[role])
        except (minisign.SignatureError, UnicodeDecodeError) as exc:
            raise UpdateError("signature check failed for role '%s': %s. Nothing was changed."
                              % (role, exc))
        sigs[role] = sig_raw
    try:
        manifest = validate_manifest(json.loads(manifest_raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("manifest is not valid JSON: %s" % exc)
    if sorted(manifest["signing"]["roles"]) != sorted(trusted):
        raise UpdateError("manifest claims signing roles %s but this client requires %s"
                          % (sorted(manifest["signing"]["roles"]), sorted(trusted)))
    if manifest.get("key_rotation"):
        warnings.append("manifest announces a key rotation; key rotation is not supported yet "
                        "and was ignored")

    # 2. policy checks on the verified manifest.
    floor = version_floor(state)
    version = manifest["version"]
    if version <= floor:
        if version < floor:
            warnings.append("server offers release %d, older than the installed floor %d; ignored"
                            % (version, floor))
        return Result("up_to_date", version, floor, warnings=warnings)
    if manifest["rules_schema"] > SUPPORTED_RULES_SCHEMA:
        raise UpdateError("release %d uses rules schema %d; this ush supports up to %d. "
                          "Upgrade ush." % (version, manifest["rules_schema"], SUPPORTED_RULES_SCHEMA))
    if _semver(manifest["min_client"]) > _semver(client_version):
        raise UpdateError("release %d needs ush >= %s (this is %s). Upgrade ush."
                          % (version, manifest["min_client"], client_version))
    if now > manifest["_expires"]:
        warnings.append("release %d expired on %s: the publisher may have stopped updating"
                        % (version, manifest["expires"]))

    # 3. every listed file must match the signed size and hash.
    files = {}
    for entry in manifest["files"]:
        data = fetch("%s/%s" % (base, entry["path"]), entry["size"])
        if len(data) != entry["size"]:
            raise UpdateError("%s: size %d does not match the signed manifest (%d)"
                              % (entry["path"], len(data), entry["size"]))
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise UpdateError("%s: SHA-256 does not match the signed manifest" % entry["path"])
        files[entry["path"]] = data

    # 4. the rules themselves must pass the client's own validation.
    try:
        new_data = json.loads(files["rules.json"].decode("utf-8"))
        Rules(new_data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("rules.json is not valid JSON: %s" % exc)
    except RulesError as exc:
        raise UpdateError("rules.json failed validation, nothing installed:\n  %s"
                          % "\n  ".join(exc.errors[:10]))
    diff = diff_rules(_current_rules_data(state), new_data)

    if dry_run:
        return Result("dry_run", version, floor, diff, warnings)

    # 5. keep the proof next to the rules, then install atomically.
    files["manifest.json"] = manifest_raw
    for role, sig_raw in sigs.items():
        files["manifest.json.%s.minisig" % role] = sig_raw
    install_release(state, version, files)
    return Result("installed", version, floor, diff, warnings)
