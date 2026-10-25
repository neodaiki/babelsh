"""Tests for host probing, plan execution/confirmation and the generated shell hook."""
import io
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "compiler"))

from compile_rules import compile_dir  # noqa: E402
from babelsh import Plan, Rules, Step, translate  # noqa: E402
from babelsh.cli import _UI, handle_plan  # noqa: E402
from babelsh.hook import generate  # noqa: E402
from babelsh.host import detect_family, probe_host  # noqa: E402

EXAMPLES = os.path.join(ROOT, "spec", "examples")
BIN = os.path.join(ROOT, "bin", "ush")
RULES = Rules(compile_dir(EXAMPLES))


class Result(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class HostProbeTests(unittest.TestCase):
    def probe(self, system, osr=None, gnu_sed=True, euid=1000, present=("sudo",), mac_ver="14.5"):
        def which(cmd):
            return "/bin/" + cmd if cmd in present else None

        def run(argv):
            out = b"sed (GNU sed) 4.8" if gnu_sed else b""
            return Result(stdout=out, stderr=b"" if gnu_sed else b"sed: illegal option -- -")

        return probe_host(RULES, system=system, machine="arm64" if system == "Darwin" else "x86_64",
                          osr=osr or {}, which=which, run=run, geteuid=lambda: euid,
                          mac_ver=lambda: mac_ver, exists=lambda p: p == "/opt/homebrew/bin/brew")

    def test_family_detection(self):
        self.assertEqual(detect_family("Linux", {"ID": "ubuntu", "ID_LIKE": "debian"}), "debian")
        self.assertEqual(detect_family("Linux", {"ID": "linuxmint", "ID_LIKE": "ubuntu debian"}), "debian")
        self.assertEqual(detect_family("Linux", {"ID": "rocky", "ID_LIKE": "rhel centos fedora"}), "fedora")
        self.assertEqual(detect_family("Linux", {"ID": "fedora"}), "fedora")
        self.assertIsNone(detect_family("Linux", {"ID": "arch"}))
        self.assertIsNone(detect_family("FreeBSD", {}))
        self.assertEqual(detect_family("Darwin", {}), "macos")

    def test_macos_profile(self):
        h = self.probe("Darwin", present=("sudo", "sed", "brew"), gnu_sed=False)
        self.assertEqual((h["family"], h["os_id"], h["arch"]), ("macos", "macos-14", "arm64"))
        self.assertEqual(h["dialect"], {"sed": "bsd"})
        self.assertEqual(h["brew_prefix"], "/opt/homebrew")
        self.assertTrue(h["has"]["brew"])
        self.assertFalse(h["has"]["apt"])

    def test_debian_profile_and_root(self):
        h = self.probe("Linux", {"ID": "debian", "VERSION_ID": "12"}, present=("sudo", "sed", "apt"),
                       euid=0)
        self.assertEqual((h["family"], h["os_id"], h["is_root"]), ("debian", "debian-12", True))
        self.assertEqual(h["dialect"], {"sed": "gnu"})
        self.assertEqual(h["arch"], "amd64")

    def test_missing_tool_has_no_dialect(self):
        h = self.probe("Linux", {"ID": "debian", "VERSION_ID": "12"}, present=("sudo",))
        self.assertEqual(h["dialect"], {})


def run_in_pty(argv, env, answers=(), timeout=30):
    """Run argv with a real controlling terminal (pty). When `prompt` appears in the output,
    type `reply`. Returns (exit status, decoded output). Stdlib only; works on Linux and macOS."""
    pid, fd = pty.fork()
    if pid == 0:                      # child: becomes session leader with the pty as its terminal
        try:
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    out, pending = b"", list(answers)
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:               # EIO once the child has exited
            break
        if not chunk:
            break
        out += chunk
        if pending and pending[0][0].encode() in out:
            os.write(fd, pending.pop(0)[1].encode())
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status), out.decode("utf-8", "replace")


class RealTerminalTests(unittest.TestCase):
    """The confirmation prompt must work on a real terminal. Unit tests inject `ask`, which is how
    a broken /dev/tty handling (text-mode 'r+' raises UnsupportedOperation) went unnoticed."""

    def ask(self, reply):
        code = ("import sys; sys.path.insert(0, %r); from babelsh.cli import _ask_tty; "
                "print('ANSWER=' + repr(_ask_tty('Q? ')))" % ROOT)
        status, out = run_in_pty([sys.executable, "-c", code], dict(os.environ),
                                 [("Q? ", reply)])
        return out

    def test_answer_is_read_from_the_terminal(self):
        self.assertIn("ANSWER='y'", self.ask("y\n"))

    def test_empty_answer_is_an_empty_string_not_none(self):
        self.assertIn("ANSWER=''", self.ask("\n"))

    def test_prompt_is_written_to_the_terminal(self):
        self.assertIn("Q? ", self.ask("n\n"))


class ExplainTests(unittest.TestCase):
    MAC = {"family": "macos", "dialect": {"sed": "bsd"}, "has": {"brew": True, "sudo": True},
           "is_root": False}

    def explain(self, args):
        import contextlib
        from babelsh.cli import cmd_explain
        out = io.StringIO()
        env = {"USH_HOST": json.dumps(self.MAC), "USH_RULES": os.path.join(ROOT, "babelsh", "data", "rules.json")}
        with contextlib.redirect_stdout(out):
            cmd_explain(args, env)
        return out.getvalue()

    def test_split_arguments_keep_their_boundaries(self):
        # `ush explain sed -i '' s/a/b/ f` arrives as separate argv items including ""
        self.assertIn("command:  sed -i '' s/a/b/ f", self.explain(["sed", "-i", "s/a/b/", "f"]))
        self.assertIn("status:   passthrough", self.explain(["sed", "-i", "", "s/a/b/", "f"]))

    def test_single_argument_is_a_whole_command_line(self):
        out = self.explain(["apt install -y nginx && systemctl start nginx"])
        self.assertIn("brew install nginx && brew services start nginx", out)


class HandlePlanTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.out = io.StringIO()
        self.ui = _UI(self.out, False)

    def call(self, argv):
        self.calls.append(argv)
        return 0

    def run_plan(self, plan, answer, env=None):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answer

        rc = handle_plan(plan, ["x"], env or {}, ui=self.ui, ask=ask, call=self.call)
        return rc, asked

    def plan(self, confirm, steps=(["brew", "install", "a"],), joins=()):
        return Plan("translated", "apt install a", [Step(s) for s in steps], list(joins),
                    "exact", "modify", confirm)

    def test_yn_requires_y(self):
        rc, _ = self.run_plan(self.plan("yn"), "")
        self.assertEqual((rc, self.calls), (1, []))
        rc, _ = self.run_plan(self.plan("yn"), "y")
        self.assertEqual((rc, self.calls), (0, [["brew", "install", "a"]]))

    def test_enter_accepts_empty_but_not_n(self):
        self.assertEqual(self.run_plan(self.plan("enter"), "n")[0], 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_plan(self.plan("enter"), "")[0], 0)

    def test_typed_needs_exact_yes(self):
        for answer in ("y", "Yes", ""):
            self.calls[:] = []
            self.assertEqual(self.run_plan(self.plan("typed"), answer)[0], 1, answer)
            self.assertEqual(self.calls, [])
        self.assertEqual(self.run_plan(self.plan("typed"), "yes")[0], 0)

    def test_no_terminal_refuses(self):
        rc, _ = self.run_plan(self.plan("yn"), None)
        self.assertEqual((rc, self.calls), (125, []))

    def test_assume_yes_covers_nondestructive_only(self):
        rc, _ = self.run_plan(self.plan("yn"), None, {"USH_ASSUME_YES": "1"})
        self.assertEqual((rc, len(self.calls)), (0, 1))
        self.calls[:] = []
        rc, _ = self.run_plan(self.plan("typed"), None, {"USH_ASSUME_YES": "1"})
        self.assertEqual((rc, self.calls), (125, []))

    def test_dry_run_runs_nothing_and_asks_nothing(self):
        rc, asked = self.run_plan(self.plan("yn"), "y", {"USH_DRY_RUN": "1"})
        self.assertEqual((rc, self.calls, asked), (0, [], []))

    def test_and_chain_stops_on_failure(self):
        results = iter([3, 0])
        self.call = lambda argv: (self.calls.append(argv), next(results))[1]
        plan = self.plan("yn", steps=(["a"], ["b"]), joins=["&&"])
        rc, _ = self.run_plan(plan, "y")
        self.assertEqual((rc, self.calls), (3, [["a"]]))

    def test_passthrough_runs_original_argv_without_ui(self):
        rc = handle_plan(Plan("passthrough", "sed -n p"), ["sed", "-n", "p"], {}, ui=self.ui,
                         ask=lambda p: self.fail("must not ask"), call=self.call)
        self.assertEqual((rc, self.calls, self.out.getvalue()), (0, [["sed", "-n", "p"]], ""))

    def test_refused_and_unsupported_run_nothing(self):
        r = handle_plan(Plan("refused", "x", reason="unknown_name"), ["x"], {}, ui=self.ui,
                        call=self.call)
        u = handle_plan(Plan("unsupported", "x"), ["x"], {}, ui=self.ui, call=self.call)
        self.assertEqual((r, u, self.calls), (127, 1, []))


def _bash_major():
    exe = shutil.which("bash")
    if not exe:
        return 0
    out = subprocess.run([exe, "-c", "echo ${BASH_VERSINFO[0]}"], stdout=subprocess.PIPE,
                         universal_newlines=True).stdout.strip()
    return int(out) if out.isdigit() else 0


class HookScenarios(object):
    """Drives the real generated hook with a sandboxed PATH on a pretend-macOS host.
    Mixed into one TestCase per shell (not a TestCase itself, so a skipped shell cannot
    skip the others)."""

    SHELL = "bash"
    SHELL_ARGS = ["--norc", "--noprofile"]
    HANDLER = "command_not_found_handle"
    NOT_FOUND = "gti: command not found"

    MAC = {"family": "macos", "os_id": "macos-14", "arch": "arm64", "dialect": {"sed": "bsd"},
           "has": {"brew": True, "sudo": True, "sed": True, "lsof": True}, "is_root": False}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        sandbox = os.path.join(cls.tmp, "bin")
        os.makedirs(sandbox)
        os.symlink(shutil.which("cat"), os.path.join(sandbox, "cat"))
        for name in ("sudo", "sed"):
            path = os.path.join(sandbox, name)
            with open(path, "w") as fh:
                fh.write('#!/bin/sh\necho "REAL-%s $*"\n' % name.upper())
            os.chmod(path, 0o755)
        cls.sandbox = sandbox
        cls.hook = os.path.join(cls.tmp, "hook." + cls.SHELL)
        with open(cls.hook, "w") as fh:
            fh.write(generate(cls.SHELL, RULES, [sys.executable, BIN]))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def sh(self, script, env=None, prelude="", hook=None):
        e = {"PATH": self.sandbox, "USH_FORCE": "1", "USH_RULES": os.path.join(ROOT, "babelsh", "data", "rules.json"),
             "USH_HOST": json.dumps(self.MAC), "USH_DRY_RUN": "1", "NO_COLOR": "1"}
        e.update(env or {})
        res = subprocess.run([shutil.which(self.SHELL)] + self.SHELL_ARGS + ["-c",
                              prelude + "source %s\n%s" % (hook or self.hook, script)],
                             env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, start_new_session=True, timeout=30)
        return res.returncode, res.stdout, res.stderr

    def pty_sh(self, script, answers):
        e = {"PATH": self.sandbox, "USH_FORCE": "1", "NO_COLOR": "1", "TERM": "dumb",
             "USH_RULES": os.path.join(ROOT, "babelsh", "data", "rules.json"),
             "USH_HOST": json.dumps(self.MAC)}
        argv = [shutil.which(self.SHELL)] + self.SHELL_ARGS + [
            "-c", "source %s\n%s" % (self.hook, script)]
        return run_in_pty(argv, e, answers)

    def test_confirming_over_a_real_terminal_runs_the_translation(self):
        status, out = self.pty_sh("sed -i 's/a/b/' f.txt", [("[y/N] ", "y\n")])
        self.assertIn("REAL-SED -i  s/a/b/ f.txt", out)       # the fake BSD-style sed ran
        self.assertEqual(status, 0)

    def test_declining_over_a_real_terminal_runs_nothing(self):
        status, out = self.pty_sh("sed -i 's/a/b/' f.txt", [("[y/N] ", "n\n")])
        self.assertNotIn("REAL-SED", out)
        self.assertIn("cancelled", out)
        self.assertEqual(status, 1)

    def test_module_launcher_form_works_without_bin_ush(self):
        # What a pip-installed babelsh generates: `python -m babelsh`, no checkout path.
        hook = os.path.join(self.tmp, "module-hook." + self.SHELL)
        with open(hook, "w") as fh:
            fh.write(generate(self.SHELL, RULES, [sys.executable, "-m", "babelsh"]))
        rc, out, _ = self.sh("apt install -y nginx", env={"PYTHONPATH": ROOT}, hook=hook)
        self.assertEqual(out, "ush-dry-run: brew install nginx\n")

    def test_generated_hook_is_valid_bash(self):
        res = subprocess.run([shutil.which(self.SHELL), "-n", self.hook])
        self.assertEqual(res.returncode, 0)

    def test_foreign_command_is_translated(self):
        rc, out, _ = self.sh("apt install -y nginx")
        self.assertEqual((rc, out), (0, "ush-dry-run: brew install nginx\n"))

    def test_sudo_prefix_foreign_command(self):
        rc, out, _ = self.sh("sudo apt install -y nginx")
        self.assertEqual(out, "ush-dry-run: brew install nginx\n")

    def test_sudo_brew_is_fixed_up(self):
        rc, out, _ = self.sh("sudo brew install htop")
        self.assertEqual(out, "ush-dry-run: brew install htop\n")

    def test_plain_sudo_goes_to_the_real_sudo_without_python(self):
        rc, out, _ = self.sh("sudo -v; sudo ls")
        self.assertEqual(out, "REAL-SUDO -v\nREAL-SUDO ls\n")

    def test_sed_with_inplace_flag_is_translated_to_bsd(self):
        rc, out, _ = self.sh("sed -i 's/a/b/' f.txt")
        self.assertEqual(out, "ush-dry-run: sed -i '' s/a/b/ f.txt\n")

    def test_sed_without_dialect_flag_never_calls_ush(self):
        rc, out, _ = self.sh("sed -n p f", env={"USH_BIN_BROKEN": "1"})
        self.assertEqual(out, "REAL-SED -n p f\n")

    def test_typo_is_a_normal_command_not_found(self):
        rc, out, err = self.sh("gti status")
        self.assertEqual(rc, 127)
        self.assertIn(self.NOT_FOUND, err)

    def test_previous_handler_is_chained(self):
        prelude = '%s() { echo "PREV $1"; return 127; }\n' % self.HANDLER
        rc, out, _ = self.sh("gti status; apt install -y nginx", prelude=prelude)
        self.assertEqual(out, "PREV gti\nush-dry-run: brew install nginx\n")

    def test_disable_switch(self):
        rc, out, err = self.sh("apt install -y nginx", env={"USH_DISABLE": "1"})
        self.assertEqual(rc, 127)
        self.assertEqual(out, "")

    def test_unsupported_and_refused_statuses(self):
        rc, _, err = self.sh("systemctl start ssh")
        self.assertEqual(rc, 1)
        rc, _, err = self.sh("apt install no-such-pkg-1")
        self.assertEqual(rc, 127)
        self.assertIn("no-such-pkg-1", err)

    def test_chain_and_pipe_are_handled_by_the_shell(self):
        rc, out, _ = self.sh("apt install -y nginx && systemctl start nginx | cat")
        self.assertEqual(out, "ush-dry-run: brew install nginx\nush-dry-run: brew services start nginx\n")

    def test_no_tty_means_no_execution(self):
        rc, out, err = self.sh("apt install -y nginx", env={"USH_DRY_RUN": ""})
        self.assertEqual(rc, 125)
        self.assertIn("no terminal to confirm", err)

    def test_shell_expanded_arguments_are_visible_to_ush(self):
        rc, out, _ = self.sh('P=nginx; apt install -y $P')
        self.assertEqual(out, "ush-dry-run: brew install nginx\n")


@unittest.skipUnless(_bash_major() >= 4,
                     "bash >= 4 needed for command_not_found_handle (macOS ships 3.2)")
class BashHookTests(HookScenarios, unittest.TestCase):
    pass


@unittest.skipUnless(shutil.which("zsh"), "zsh not available")
class ZshHookTests(HookScenarios, unittest.TestCase):
    """Same scenarios through the zsh hook."""

    SHELL = "zsh"
    SHELL_ARGS = ["-f"]
    HANDLER = "command_not_found_handler"
    NOT_FOUND = "command not found: gti"


if __name__ == "__main__":
    unittest.main()
