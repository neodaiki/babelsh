#!/usr/bin/env python3
"""Build a signed rules release (the directory that `ush update` downloads).

  release.py keygen --out-dir DIR                       # DEV key only
  release.py build RULES_DIR OUT_DIR --dev-key DIR/release.key [--version N] [--expires-days 90]
  release.py build RULES_DIR OUT_DIR --minisign-key ~/.minisign/ush.key   # production

Production releases must be signed with the real `minisign` binary and an offline key; the
built-in signer is pure Python, not constant-time, and exists for tests and local trials.
Output: manifest.json, manifest.json.release.minisig, rules.json
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compile_rules import compile_dir, dumps  # noqa: E402
from ush import ed25519  # noqa: E402
from ush._version import __version__  # noqa: E402

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def keygen(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    seed, keyid = os.urandom(32), os.urandom(8)
    key_path = os.path.join(out_dir, "release.key")
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"seed": seed.hex(), "keyid": keyid.hex(), "dev_only": True}, fh)
    pub_line = base64.b64encode(b"Ed" + keyid + ed25519.public_key(seed)).decode()
    with open(os.path.join(out_dir, "release.pub"), "w") as fh:
        fh.write("untrusted comment: minisign public key: %s\n%s\n" % (keyid[::-1].hex().upper(), pub_line))
    return key_path, pub_line


def sign_text(key_path, message, trusted_comment):
    """minisign-format ('ED', prehashed) signature file text, signed with a dev key."""
    with open(key_path) as fh:
        k = json.load(fh)
    seed, keyid = bytes.fromhex(k["seed"]), bytes.fromhex(k["keyid"])
    sig = ed25519.sign(seed, hashlib.blake2b(message, digest_size=64).digest())
    global_sig = ed25519.sign(seed, sig + trusted_comment.encode("utf-8"))
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return "untrusted comment: signature from ush dev key\n%s\ntrusted comment: %s\n%s\n" % (
        b64(b"ED" + keyid + sig), trusted_comment, b64(global_sig))


def build_release(rules_dir, out_dir, dev_key=None, minisign_key=None, version=None,
                  expires_days=90, now=None, min_client=__version__, role="release"):
    if bool(dev_key) == bool(minisign_key):
        raise SystemExit("give exactly one of --dev-key / --minisign-key")
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    rules = compile_dir(rules_dir)
    rules_bytes = dumps(rules).encode("utf-8")
    version = version or int(now.strftime("%Y%m%d")) * 100 + 1
    try:
        commit = subprocess.check_output(["git", "-C", rules_dir, "rev-parse", "HEAD"],
                                         stderr=subprocess.DEVNULL).decode().strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    manifest = {
        "format": "ush-rules-release",
        "rules_schema": rules["schema"],
        "version": version,
        "released_at": now.strftime(TS_FORMAT),
        "expires": (now + datetime.timedelta(days=expires_days)).strftime(TS_FORMAT),
        "min_client": min_client,
        "source_commit": commit,
        "signing": {"scheme": "minisign", "roles": [role]},
        "files": [{"path": "rules.json", "sha256": hashlib.sha256(rules_bytes).hexdigest(),
                   "size": len(rules_bytes)}],
        "stats": {"concepts": len(rules["concepts"]), "services": len(rules["services"]),
                  "intents": len(rules["intents"])},
        "key_rotation": None,
    }
    manifest_bytes = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")
    os.makedirs(out_dir, exist_ok=True)
    paths = {n: os.path.join(out_dir, n) for n in ("manifest.json", "rules.json")}
    sig_path = os.path.join(out_dir, "manifest.json.%s.minisig" % role)
    with open(paths["rules.json"], "wb") as fh:
        fh.write(rules_bytes)
    with open(paths["manifest.json"], "wb") as fh:
        fh.write(manifest_bytes)
    comment = "timestamp:%d file:manifest.json version:%d" % (
        (now - datetime.datetime(1970, 1, 1)).total_seconds(), version)
    if dev_key:
        with open(sig_path, "w") as fh:
            fh.write(sign_text(dev_key, manifest_bytes, comment))
    else:
        subprocess.check_call(["minisign", "-S", "-s", minisign_key, "-m", paths["manifest.json"],
                               "-x", sig_path, "-t", comment])
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out-dir", required=True)
    b = sub.add_parser("build")
    b.add_argument("rules_dir")
    b.add_argument("out_dir")
    b.add_argument("--dev-key")
    b.add_argument("--minisign-key")
    b.add_argument("--version", type=int)
    b.add_argument("--expires-days", type=int, default=90)
    args = ap.parse_args(argv)
    if args.cmd == "keygen":
        key_path, pub_line = keygen(args.out_dir)
        print("secret key (DEV ONLY, keep private): %s" % key_path)
        print("public key line for trusted_keys.json:\n%s" % pub_line)
        return 0
    m = build_release(args.rules_dir, args.out_dir, args.dev_key, args.minisign_key, args.version,
                      args.expires_days)
    print("built release %d in %s (expires %s)" % (m["version"], args.out_dir, m["expires"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
