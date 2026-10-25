"""Signing, verification and the `ush update` flow. All local: file:// plus one loopback HTTP server."""
import base64
import contextlib
import datetime
import functools
import hashlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "compiler"))

import release  # noqa: E402
from babelsh import cli, ed25519, load_rules, minisign, translate, update as upd  # noqa: E402

EXAMPLES = os.path.join(ROOT, "spec", "examples")
HOST = {"family": "macos", "dialect": {"sed": "bsd"}, "has": {"brew": True, "sudo": True},
        "is_root": False}


def write(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)


class Env(object):
    """A throwaway publisher (keys + releases) and client (config dir)."""

    def __init__(self, test):
        self.tmp = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.tmp)
        self.keys = os.path.join(self.tmp, "keys")
        self.key_path, self.pub_line = release.keygen(self.keys)
        self.trusted = os.path.join(self.tmp, "trusted.json")
        write(self.trusted, json.dumps({"release": [self.pub_line]}))
        self.config = os.path.join(self.tmp, "config")
        self.environ = {"XDG_CONFIG_HOME": self.config, "USH_TRUSTED_KEYS": self.trusted}
        self.state = os.path.join(self.config, "ush")
        self.n = 0

    def rules_dir(self, extra_package=None):
        d = os.path.join(self.tmp, "rules%d" % self.n)
        self.n += 1
        shutil.copytree(EXAMPLES, d)
        if extra_package:
            path = os.path.join(d, "packages.yaml")
            with open(path) as fh:
                text = fh.read()
            line = "  %s: { debian: %s, fedora: %s, macos: %s }\n" % ((extra_package,) * 4)
            with open(path, "w") as fh:
                fh.write(text.replace("packages:\n", "packages:\n" + line, 1))
        return d

    def release(self, version, name=None, extra_package=None, **kw):
        out = os.path.join(self.tmp, name or "rel%d" % version)
        release.build_release(self.rules_dir(extra_package), out, dev_key=self.key_path,
                              version=version, **kw)
        return out

    def url(self, out):
        return "file://" + out

    def update(self, out, **kw):
        return upd.update(self.url(out), self.environ, **kw)

    def resign(self, out, mutate_manifest=None, rules_bytes=None, key_path=None):
        """Re-write a release with a valid signature after tampering with its content."""
        if rules_bytes is not None:
            write(os.path.join(out, "rules.json"), rules_bytes)
        with open(os.path.join(out, "manifest.json")) as fh:
            m = json.load(fh)
        if rules_bytes is not None:
            m["files"][0].update(sha256=hashlib.sha256(rules_bytes).hexdigest(), size=len(rules_bytes))
        if mutate_manifest:
            mutate_manifest(m)
        raw = (json.dumps(m, indent=1, sort_keys=True) + "\n").encode()
        write(os.path.join(out, "manifest.json"), raw)
        write(os.path.join(out, "manifest.json.release.minisig"),
              release.sign_text(key_path or self.key_path, raw, "timestamp:1 file:manifest.json"))


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_vector_1(self):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb88"
                            "21590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(ed25519.public_key(seed), pub)
        self.assertEqual(ed25519.sign(seed, b""), sig)
        self.assertTrue(ed25519.verify(pub, b"", sig))
        self.assertFalse(ed25519.verify(pub, b"x", sig))

    def test_rejects_malformed_inputs(self):
        seed = bytes(range(32))
        pub, sig = ed25519.public_key(seed), ed25519.sign(seed, b"m")
        self.assertFalse(ed25519.verify(pub[:31], b"m", sig))
        self.assertFalse(ed25519.verify(pub, b"m", sig[:63]))
        flipped = bytearray(sig)
        flipped[40] ^= 1
        self.assertFalse(ed25519.verify(pub, b"m", bytes(flipped)))
        # s >= group order must be rejected (signature malleability)
        order = 2 ** 252 + 27742317777372353535851937790883648493
        s = int.from_bytes(sig[32:], "little") + order
        self.assertFalse(ed25519.verify(pub, b"m", sig[:32] + s.to_bytes(32, "little")))

    def test_agrees_with_cryptography_library(self):
        try:
            from cryptography.hazmat.primitives import serialization as ser
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("cryptography not installed")
        for i in range(15):
            seed, msg = os.urandom(32), os.urandom(i * 11)
            key = Ed25519PrivateKey.from_private_bytes(seed)
            pub = key.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
            self.assertEqual(ed25519.public_key(seed), pub)
            self.assertEqual(ed25519.sign(seed, msg), key.sign(msg))
            self.assertTrue(ed25519.verify(pub, msg, key.sign(msg)))


class MinisignTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.msg = b"hello manifest"
        self.sig = release.sign_text(self.env.key_path, self.msg, "timestamp:1 file:x")

    def test_roundtrip_returns_trusted_comment(self):
        self.assertEqual(minisign.verify(self.msg, self.sig, [self.env.pub_line]),
                         "timestamp:1 file:x")

    def test_accepts_full_pub_file_text(self):
        with open(os.path.join(self.env.keys, "release.pub")) as fh:
            minisign.verify(self.msg, self.sig, [fh.read()])

    def test_tampered_message_fails(self):
        with self.assertRaises(minisign.SignatureError):
            minisign.verify(self.msg + b"!", self.sig, [self.env.pub_line])

    def test_tampered_trusted_comment_fails(self):
        forged = self.sig.replace("timestamp:1", "timestamp:2")
        with self.assertRaisesRegex(minisign.SignatureError, "does not match"):
            minisign.verify(self.msg, forged, [self.env.pub_line])

    def test_unknown_key_fails(self):
        other = Env(self)
        with self.assertRaisesRegex(minisign.SignatureError, "unknown key"):
            minisign.verify(self.msg, self.sig, [other.pub_line])

    def test_any_pinned_key_may_match(self):
        other = Env(self)
        minisign.verify(self.msg, self.sig, [other.pub_line, self.env.pub_line])

    def test_legacy_non_prehashed_variant(self):
        with open(self.env.key_path) as fh:
            k = json.load(fh)
        seed, keyid = bytes.fromhex(k["seed"]), bytes.fromhex(k["keyid"])
        sig = ed25519.sign(seed, self.msg)
        gsig = ed25519.sign(seed, sig + b"c")
        b64 = lambda r: base64.b64encode(r).decode()  # noqa: E731
        text = "untrusted comment: x\n%s\ntrusted comment: c\n%s\n" % (b64(b"Ed" + keyid + sig), b64(gsig))
        self.assertEqual(minisign.verify(self.msg, text, [self.env.pub_line]), "c")

    def test_malformed_inputs(self):
        for bad in ("", "garbage", "untrusted comment: x\n!!!\ntrusted comment: c\nAAAA\n"):
            with self.assertRaises(minisign.SignatureError):
                minisign.verify(self.msg, bad, [self.env.pub_line])
        with self.assertRaises(minisign.SignatureError):
            minisign.parse_public_key("not base64 !!")


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def assertNothingInstalled(self):
        self.assertFalse(os.path.exists(os.path.join(self.env.state, "current")))
        self.assertFalse(os.path.isdir(os.path.join(self.env.state, "releases")) and
                         os.listdir(os.path.join(self.env.state, "releases")))

    def test_installs_and_layout(self):
        res = self.env.update(self.env.release(2026102601))
        self.assertEqual((res.status, res.version), ("installed", 2026102601))
        s = self.env.state
        self.assertEqual(os.readlink(os.path.join(s, "current")), os.path.join("releases", "2026102601"))
        for name in ("rules.json", "manifest.json", "manifest.json.release.minisig"):
            self.assertTrue(os.path.isfile(os.path.join(s, "current", name)), name)
        with open(os.path.join(s, "high_water")) as fh:
            self.assertEqual(fh.read().strip(), "2026102601")

    def test_installed_rules_are_what_the_client_loads_and_uses(self):
        self.env.update(self.env.release(5, extra_package="wget"))
        path = cli.find_rules_path(self.env.environ)
        self.assertEqual(path, os.path.join(self.env.state, "current", "rules.json"))
        plan = translate("apt install wget", HOST, load_rules(path))
        self.assertEqual([s.argv for s in plan.steps], [["brew", "install", "wget"]])

    def test_diff_describes_the_change(self):
        res = self.env.update(self.env.release(5, extra_package="wget"), dry_run=True)
        self.assertEqual(res.diff, ["+ package wget"])

    def test_dry_run_changes_nothing(self):
        res = self.env.update(self.env.release(5), dry_run=True)
        self.assertEqual(res.status, "dry_run")
        self.assertNothingInstalled()

    def test_same_version_is_up_to_date(self):
        out = self.env.release(5)
        self.env.update(out)
        self.assertEqual(self.env.update(out).status, "up_to_date")

    def test_newer_replaces_and_keeps_previous(self):
        self.env.update(self.env.release(5))
        self.env.update(self.env.release(6))
        self.assertEqual(os.readlink(os.path.join(self.env.state, "previous")), os.path.join("releases", "5"))
        self.assertEqual(os.readlink(os.path.join(self.env.state, "current")), os.path.join("releases", "6"))

    def test_older_release_is_ignored_even_if_validly_signed(self):
        old = self.env.release(5)
        self.env.update(self.env.release(6))
        res = self.env.update(old)
        self.assertEqual(res.status, "up_to_date")
        self.assertTrue(any("older" in w for w in res.warnings))
        self.assertEqual(upd.installed_manifest(self.env.state)["version"], 6)

    def test_version_floor_survives_deleting_current(self):
        old = self.env.release(5)
        self.env.update(self.env.release(6))
        os.unlink(os.path.join(self.env.state, "current"))
        shutil.rmtree(os.path.join(self.env.state, "releases"))
        self.assertEqual(self.env.update(old).status, "up_to_date")  # high_water file still says 6

    def test_rollback_swaps_and_floor_stays(self):
        v5, v6 = self.env.release(5), self.env.release(6)
        self.env.update(v5)
        self.env.update(v6)
        self.assertEqual(upd.rollback(self.env.state), "5")
        self.assertEqual(upd.installed_manifest(self.env.state)["version"], 5)
        self.assertEqual(upd.version_floor(self.env.state), 6)
        self.assertEqual(self.env.update(v6).status, "up_to_date")   # v6 not re-offered as "new"
        self.assertEqual(upd.rollback(self.env.state), "6")           # but can be rolled forward

    def test_rollback_without_previous_fails(self):
        self.env.update(self.env.release(5))
        with self.assertRaisesRegex(upd.UpdateError, "no previous"):
            upd.rollback(self.env.state)

    def test_old_releases_are_pruned(self):
        for v in range(1, 7):
            self.env.update(self.env.release(v))
        kept = sorted(os.listdir(os.path.join(self.env.state, "releases")), key=int)
        self.assertEqual(kept, ["4", "5", "6"])

    # -- attacks and failures: nothing may change ---------------------
    def test_tampered_manifest_is_rejected(self):
        out = self.env.release(5)
        with open(os.path.join(out, "manifest.json")) as fh:
            raw = fh.read().replace('"version": 5', '"version": 9')
        write(os.path.join(out, "manifest.json"), raw)
        with self.assertRaisesRegex(upd.UpdateError, "signature check failed"):
            self.env.update(out)
        self.assertNothingInstalled()

    def test_tampered_rules_are_rejected(self):
        out = self.env.release(5)
        with open(os.path.join(out, "rules.json"), "rb") as fh:
            data = fh.read()
        write(os.path.join(out, "rules.json"), data.replace(b"nginx", b"nginy", 1))
        with self.assertRaisesRegex(upd.UpdateError, "SHA-256"):
            self.env.update(out)
        self.assertNothingInstalled()

    def test_oversized_file_is_rejected(self):
        out = self.env.release(5)
        with open(os.path.join(out, "rules.json"), "ab") as fh:
            fh.write(b" " * 10)
        with self.assertRaisesRegex(upd.UpdateError, "larger than"):
            self.env.update(out)
        self.assertNothingInstalled()

    def test_release_signed_by_another_key_is_rejected(self):
        other = Env(self)
        out = self.env.release(5)
        self.env.resign(out, key_path=other.key_path)
        with self.assertRaisesRegex(upd.UpdateError, "unknown key"):
            self.env.update(out)
        self.assertNothingInstalled()

    def test_missing_signature_file_is_rejected(self):
        out = self.env.release(5)
        os.unlink(os.path.join(out, "manifest.json.release.minisig"))
        with self.assertRaisesRegex(upd.UpdateError, "cannot fetch"):
            self.env.update(out)

    def test_signed_but_invalid_rules_are_rejected(self):
        out = self.env.release(5)
        with open(os.path.join(out, "rules.json")) as fh:
            bad = json.load(fh)
        bad["concepts"]["htop"]["targets"]["macos"] = [{"pkg": "htop; reboot"}]
        self.env.resign(out, rules_bytes=release.dumps(bad).encode())
        with self.assertRaisesRegex(upd.UpdateError, "failed validation"):
            self.env.update(out)
        self.assertNothingInstalled()

    def test_signed_non_json_rules_are_rejected(self):
        out = self.env.release(5)
        self.env.resign(out, rules_bytes=b"not json")
        with self.assertRaisesRegex(upd.UpdateError, "not valid JSON"):
            self.env.update(out)

    def test_path_traversal_in_manifest_is_rejected(self):
        out = self.env.release(5)

        def mutate(m):
            m["files"].append({"path": "../evil", "sha256": "0" * 64, "size": 1})
        self.env.resign(out, mutate_manifest=mutate)
        with self.assertRaisesRegex(upd.UpdateError, "malformed file entry"):
            self.env.update(out)

    def test_manifest_roles_must_match_what_the_client_requires(self):
        out = self.env.release(5)
        self.env.resign(out, mutate_manifest=lambda m: m["signing"].update(roles=["release", "ci"]))
        with self.assertRaisesRegex(upd.UpdateError, "claims signing roles"):
            self.env.update(out)

    def test_two_role_client_needs_both_signatures(self):
        second = Env(self)
        write(self.env.trusted, json.dumps({"release": [self.env.pub_line], "ci": [second.pub_line]}))
        out = self.env.release(5)  # only a 'release' signature exists
        with self.assertRaisesRegex(upd.UpdateError, "cannot fetch"):
            self.env.update(out)

    def test_no_trusted_keys_configured(self):
        write(self.env.trusted, json.dumps({"release": []}))
        with self.assertRaisesRegex(upd.UpdateError, "no trusted keys"):
            self.env.update(self.env.release(5))

    def test_bundled_trust_file_ships_empty_on_purpose(self):
        keys = upd.load_trusted_keys({})
        self.assertEqual(keys, {})

    def test_client_too_old_for_release(self):
        out = self.env.release(5, min_client="99.0.0")
        with self.assertRaisesRegex(upd.UpdateError, "needs ush >= 99.0.0"):
            self.env.update(out)

    def test_rules_schema_too_new(self):
        out = self.env.release(5)
        self.env.resign(out, mutate_manifest=lambda m: m.update(rules_schema=2))
        with self.assertRaisesRegex(upd.UpdateError, "schema 2"):
            self.env.update(out)

    def test_expired_release_installs_with_warning(self):
        out = self.env.release(5, expires_days=-1)
        res = self.env.update(out)
        self.assertEqual(res.status, "installed")
        self.assertTrue(any("expired" in w for w in res.warnings))
        self.assertTrue(upd.status(self.env.state)["installed"]["stale"])

    def test_failure_during_install_keeps_previous_release(self):
        self.env.update(self.env.release(5))
        real_rename = os.rename

        def boom(src, dst):
            raise OSError("disk full")
        upd.os.rename = boom
        try:
            with self.assertRaises(OSError):
                self.env.update(self.env.release(6))
        finally:
            upd.os.rename = real_rename
        self.assertEqual(upd.installed_manifest(self.env.state)["version"], 5)
        self.assertEqual(os.listdir(os.path.join(self.env.state, "releases")), ["5"])

    def test_unsupported_url_scheme(self):
        with self.assertRaisesRegex(upd.UpdateError, "unsupported URL scheme"):
            upd.update("ftp://example.invalid/rules", self.env.environ)

    def test_https_to_http_redirect_is_refused(self):
        handler = upd._NoDowngrade()
        req = urllib.request.Request("https://example.invalid/manifest.json")
        with self.assertRaisesRegex(upd.UpdateError, "refusing redirect"):
            handler.redirect_request(req, None, 302, "Found", {}, "http://example.invalid/x")

    def test_over_http(self):
        out = self.env.release(5)
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=out))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        res = upd.update("http://127.0.0.1:%d/" % server.server_address[1], self.env.environ)
        self.assertEqual(res.status, "installed")


class VersionKeyTests(unittest.TestCase):
    def test_prerelease_ordering(self):
        order = ["0.1.0a1", "0.1.0a2", "0.1.0b1", "0.1.0rc1", "0.1.0", "0.1.1a1", "0.2.0"]
        keys = [upd._semver(v) for v in order]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))

    def test_rejects_garbage(self):
        for bad in ("1.0", "1.0.0.dev1", "v1.0.0", "1.0.0-beta", ""):
            with self.assertRaises(upd.UpdateError, msg=bad):
                upd._semver(bad)

    def test_alpha_client_is_older_than_the_final_release_it_precedes(self):
        env = Env(self)
        out = env.release(5, min_client="0.1.0")
        with self.assertRaisesRegex(upd.UpdateError, "needs ush >= 0.1.0"):
            env.update(out)          # default client_version is the current 0.1.0a1
        self.assertEqual(env.update(out, client_version="0.1.0").status, "installed")

    def test_alpha_manifest_min_client_is_accepted(self):
        env = Env(self)
        self.assertEqual(env.update(env.release(6, min_client="0.1.0a1")).status, "installed")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def run_cli(self, fn, args, environ=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(args, environ or self.env.environ)
        return rc, out.getvalue(), err.getvalue()

    def test_update_status_and_rollback_commands(self):
        v5, v6 = self.env.release(5), self.env.release(6, extra_package="wget")
        rc, out, _ = self.run_cli(cli.cmd_update, [self.env.url(v5)])
        self.assertEqual(rc, 0)
        self.assertIn("installed release 5", out)
        rc, out, _ = self.run_cli(cli.cmd_update, ["--dry-run", self.env.url(v6)])
        self.assertIn("would install release 6", out)
        self.assertIn("+ package wget", out)
        self.run_cli(cli.cmd_update, [self.env.url(v6)])
        rc, out, _ = self.run_cli(cli.cmd_rules, ["status"])
        self.assertIn("release 6", out)
        rc, out, _ = self.run_cli(cli.cmd_rules, ["rollback"])
        self.assertIn("release 5", out)

    def test_update_failure_exit_code_and_message(self):
        out = self.env.release(5)
        write(os.path.join(out, "manifest.json"), "{}")
        rc, _, err = self.run_cli(cli.cmd_update, [self.env.url(out)])
        self.assertEqual(rc, 1)
        self.assertIn("signature check failed", err)

    def test_update_needs_a_url(self):
        rc, _, err = self.run_cli(cli.cmd_update, [])
        self.assertEqual(rc, 2)
        self.assertIn("USH_RULES_URL", err)

    def test_url_from_environment(self):
        env = dict(self.env.environ, USH_RULES_URL=self.env.url(self.env.release(5)))
        rc, out, _ = self.run_cli(cli.cmd_update, [], env)
        self.assertEqual(rc, 0)
        self.assertIn("installed release 5", out)


if __name__ == "__main__":
    unittest.main()
