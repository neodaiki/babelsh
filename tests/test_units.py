"""Unit tests for the tokenizer and the rule validator. Run: python3 -m unittest discover -s tests"""
import copy
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "compiler"))

from compile_rules import compile_dir  # noqa: E402
from ush import Rules, RulesError, to_shell, translate  # noqa: E402
from ush.shellparse import ShellParseError, split  # noqa: E402

EXAMPLES = os.path.join(ROOT, "spec", "examples")


def words(text):
    return [[t.value for t in s.tokens] for s in split(text)]


class TokenizerTests(unittest.TestCase):
    def test_quotes_and_escapes(self):
        self.assertEqual(words("sed -i '' 's/a b/c/' \"f 1.txt\" x\\ y"),
                         [["sed", "-i", "", "s/a b/c/", "f 1.txt", "x y"]])

    def test_operators_and_ops(self):
        segs = split("a && b || c; d | e")
        self.assertEqual([s.op for s in segs], ["&&", "||", ";", "|", None])

    def test_operator_inside_quotes_is_literal(self):
        self.assertEqual(words("echo 'a && b'"), [["echo", "a && b"]])

    def test_dynamic_tokens(self):
        for text in ("apt install $(cat x)", "apt install `cat x`", "apt install $PKG",
                     "apt install ${PKG}", "apt install lib*", 'apt install "$PKG"'):
            seg = split(text)[0]
            self.assertTrue(any(t.dynamic for t in seg.tokens), text)

    def test_single_quoted_dollar_is_not_dynamic(self):
        seg = split("echo '$HOME'")[0]
        self.assertFalse(any(t.dynamic for t in seg.tokens))

    def test_syntax_flags(self):
        for text in ("apt install x &", "FOO=1 apt install x", "apt install x # c",
                     "(apt install x)", "apt install x > \"$f\"", "apt install x > 'a b'",
                     "cat <<EOF", "apt install x >"):
            self.assertTrue(split(text)[0].syntax, text)
        self.assertFalse(split("apt install x")[0].syntax)

    def test_simple_redirects_are_recorded_not_tokenized(self):
        seg = split("apt install x > log 2>&1")[0]
        self.assertEqual([t.value for t in seg.tokens], ["apt", "install", "x"])
        self.assertEqual(seg.redirs, ["> log".replace("> ", ">"), "2>&1"])
        self.assertFalse(seg.syntax)
        for text, want in (("a 2>/dev/null", ["2>/dev/null"]), ("a >>out", [">>out"]),
                           ("a < in", ["<in"]), ("a &> all", ["&>all"])):
            self.assertEqual(split(text)[0].redirs, want, text)
            self.assertFalse(split(text)[0].syntax, text)

    def test_redirect_does_not_swallow_numeric_argument(self):
        seg = split("kill -9 123 > /dev/null")[0]
        self.assertEqual([t.value for t in seg.tokens], ["kill", "-9", "123"])

    def test_errors(self):
        for text in ("echo 'a", 'echo "a', "echo a \\", "a && && b", "echo $(a"):
            with self.assertRaises(ShellParseError, msg=text):
                split(text)

    def test_trailing_semicolon_ok(self):
        self.assertEqual(words("ls;"), [["ls"]])

    def test_raw_text_preserved(self):
        segs = split("apt install -y nginx |  tee  $LOG")
        self.assertEqual(segs[1].raw, "tee  $LOG")


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.data = compile_dir(EXAMPLES)

    def broken(self, mutate):
        d = copy.deepcopy(self.data)
        mutate(d)
        with self.assertRaises(RulesError) as cm:
            Rules(d)
        return "\n".join(cm.exception.errors)

    def test_examples_are_valid(self):
        Rules(self.data)

    def test_shell_metachar_in_package_name(self):
        msg = self.broken(lambda d: d["concepts"]["htop"]["targets"]["macos"].__setitem__(
            0, {"pkg": "htop; rm -rf /"}))
        self.assertIn("bad package name", msg)

    def test_unknown_action_rejected(self):
        msg = self.broken(lambda d: d["concepts"]["htop"]["targets"]["macos"].__setitem__(
            0, {"action": "run_shell"}))
        self.assertIn("unknown action", msg)

    def test_cask_is_v1_unsupported(self):
        msg = self.broken(lambda d: d["concepts"]["htop"].__setitem__("kind", "cask"))
        self.assertIn("not supported in v1", msg)

    def test_reverse_index_conflict(self):
        def mutate(d):
            d["concepts"]["htop"]["targets"]["debian"] = [{"pkg": "nginx"}]
        self.assertIn("maps to both", self.broken(mutate))

    def test_flag_coverage_is_enforced(self):
        def mutate(d):
            intent = [i for i in d["intents"] if i["id"] == "pkg.install"][0]
            macos = [f for f in intent["forms"] if f["when"].get("family") == "macos"][0]
            del macos["implicit"]
        self.assertIn("must be exactly one of", self.broken(mutate))

    def test_literal_with_shell_chars_rejected(self):
        def mutate(d):
            intent = [i for i in d["intents"] if i["id"] == "pkg.update_index"][0]
            intent["forms"][0]["render"] = ["apt", "update", "; reboot"]
        self.assertIn("outside the allowed set", self.broken(mutate))

    def test_unknown_keys_rejected(self):
        def mutate(d):
            d["intents"][0]["forms"][0]["run"] = "rm -rf /"
        self.assertIn("unknown keys", self.broken(mutate))


class SerializationTests(unittest.TestCase):
    def test_json_roundtrip_preserves_argument_order(self):
        # Regression: sort_keys=True put `files` before `script` and broke every sed translation.
        import json
        from compile_rules import dumps
        data = json.loads(dumps(compile_dir(EXAMPLES)))
        sed = [i for i in data["intents"] if i["id"] == "text.sed_inplace"][0]
        self.assertEqual(list(sed["args"]), ["script", "files"])

    def test_bundled_rules_match_a_fresh_compile(self):
        import json
        from compile_rules import dumps
        path = os.path.join(ROOT, "ush", "data", "rules.json")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), dumps(compile_dir(EXAMPLES)),
                             "ush/data/rules.json is stale: run compiler/compile_rules.py "
                             "spec/examples -o ush/data/rules.json")


class BehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = Rules(compile_dir(EXAMPLES))
        cls.mac = {"family": "macos", "dialect": {"sed": "bsd"},
                   "has": {"brew": True, "sudo": True}, "is_root": False}
        cls.deb = {"family": "debian", "dialect": {"sed": "gnu"},
                   "has": {"apt": True, "sudo": True, "systemctl": True}, "is_root": False}

    def t(self, text, host=None):
        return translate(text, host or self.mac, self.rules)

    def test_simple_redirect_is_carried_over(self):
        p = self.t("apt install -y nginx > log 2>&1")
        self.assertEqual(p.status, "translated")
        self.assertEqual(to_shell(p), "brew install nginx >log 2>&1")

    def test_redirect_on_multi_step_expansion_is_refused(self):
        p = self.t("apt install libssl-dev build-essential > log")
        self.assertEqual((p.status, p.reason), ("refused", "shell_syntax"))

    def test_background_and_complex_redirect_target_are_refused(self):
        for text in ("apt install -y nginx &", 'apt install -y nginx > "$LOG"'):
            p = self.t(text)
            self.assertEqual((p.status, p.reason), ("refused", "shell_syntax"), text)

    def test_redirect_on_untouched_stage_is_fine(self):
        p = self.t("apt install -y nginx | tee > log")
        self.assertEqual(p.status, "translated")
        self.assertEqual(p.steps[1].raw, "tee > log")

    def test_sudo_with_options_is_left_alone(self):
        self.assertEqual(self.t("sudo -u bob apt install x").status, "passthrough")

    def test_apt_without_packages_is_refused(self):
        self.assertEqual(self.t("apt install").reason, "bad_arguments")

    def test_one_unknown_package_blocks_the_whole_command(self):
        p = self.t("apt install nginx nope-123")
        self.assertEqual((p.status, p.reason), ("refused", "unknown_name"))

    def test_remove_never_runs_install_only_actions(self):
        p = self.t("apt remove build-essential")
        self.assertEqual((p.status, p.steps), ("noop", []))  # no xcode-select --install either

    def test_native_command_is_never_blocked(self):
        for text in ("apt search ripgrep", "apt install --no-install-recommends x",
                     "apt install $(cat pkgs)", "apt install totally-unknown"):
            self.assertEqual(self.t(text, self.deb).status, "passthrough", text)

    def test_unmatched_sed_is_untouched(self):
        self.assertEqual(self.t("sed -n '1p' f").status, "passthrough")
        self.assertEqual(self.t("sed -E -i 's/a/b/' f").status, "passthrough")

    def test_chain_with_unsupported_segment_runs_nothing(self):
        p = self.t("apt install -y nginx && systemctl start ssh")
        self.assertEqual((p.status, p.steps), ("unsupported", []))

    def test_korean_notes(self):
        p = translate("apt install libssl-dev", self.mac, self.rules, lang="ko")
        self.assertTrue(any("LDFLAGS" in n for n in p.notes))
        self.assertFalse(any("set LDFLAGS" in n for n in p.notes))


if __name__ == "__main__":
    unittest.main()
