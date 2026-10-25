"""Command line: `ush exec -- CMD...`, `ush explain`, `ush hostinfo`, `ush init SHELL`.

`exec` is what the shell hook calls. It never goes through a shell: translated plans are run
as argv lists with subprocess, so nothing in a rule or a package name can be interpreted by
a shell.

Exit codes: the command's own status; 127 = could not translate (like "command not found");
1 = no equivalent on this host; 125 = would have run but could not ask for confirmation.
"""
import json
import os
import shlex
import subprocess
import sys

from .host import get_host
from .hook import generate
from .plan import to_shell
from .rules import load_rules
from .translate import translate
from .update import UpdateError, config_dir

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLED_RULES = os.path.join(HERE, "data", "rules.json")
EXIT_NOT_RUN, EXIT_UNSUPPORTED, EXIT_NO_TTY = 127, 1, 125


def find_rules_path(environ=None):
    environ = os.environ if environ is None else environ
    candidates = [environ.get("USH_RULES"),
                  os.path.join(config_dir(environ), "current", "rules.json"),
                  BUNDLED_RULES]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise SystemExit("ush: no rules.json found (set USH_RULES or run the compiler)")


def pick_lang(environ=None):
    environ = os.environ if environ is None else environ
    lang = environ.get("USH_LANG") or environ.get("LC_ALL") or environ.get("LANG") or ""
    return "ko" if lang.lower().startswith("ko") else "en"


class _UI(object):
    def __init__(self, stream, color):
        self.out = stream
        self.color = color

    def c(self, code, text):
        return "\033[%sm%s\033[0m" % (code, text) if self.color else text

    def line(self, text=""):
        self.out.write(text + "\n")
        self.out.flush()


def _ask_tty(prompt):
    """One line from the controlling terminal (not stdin: stdin may be a pipe). None = no answer."""
    # Separate read and write handles: a text-mode "r+" open of a tty raises
    # io.UnsupportedOperation ("not seekable"), which is an OSError and would be mistaken for
    # "no terminal".
    try:
        with open("/dev/tty", "w") as out, open("/dev/tty", "r") as inp:
            out.write(prompt)
            out.flush()
            line = inp.readline()
    except (OSError, ValueError):
        return None
    return line.rstrip("\n") if line else None


def _call(argv):
    try:
        return subprocess.call(argv)
    except FileNotFoundError:
        sys.stderr.write("ush: %s: command not found\n" % argv[0])
        return EXIT_NOT_RUN
    except PermissionError:
        sys.stderr.write("ush: %s: permission denied\n" % argv[0])
        return 126


def handle_plan(plan, argv, environ=None, ui=None, ask=_ask_tty, call=_call):
    """Act on a Plan. Returns the process exit status."""
    environ = os.environ if environ is None else environ
    ui = ui or _UI(sys.stderr, sys.stderr.isatty() and "NO_COLOR" not in environ)

    if plan.status == "passthrough":
        return call(argv)
    for n in plan.notes:
        ui.line("  " + ui.c("33", "note: ") + n)
    if plan.status == "noop":
        return 0
    if plan.status == "refused":
        ui.line(ui.c("31", "ush: not translated (%s)" % plan.reason))
        return EXIT_NOT_RUN
    if plan.status == "unsupported":
        ui.line(ui.c("31", "ush: no equivalent on this system"))
        return EXIT_UNSUPPORTED

    shown = to_shell(plan)
    risky = plan.confirm == "typed"
    ui.line("%s %s  %s  %s   %s" % (
        ui.c("36", "ush:"), plan.original, ui.c("36", "->"), ui.c("31;1" if risky else "1", shown),
        ui.c("2", "[%s, %s]" % (plan.fidelity, plan.risk))))

    if environ.get("USH_DRY_RUN"):
        sys.stdout.write("ush-dry-run: %s\n" % shown)
        return 0
    prompts = {"none": None,
               "enter": "Run? [Enter=yes, n=cancel] ",
               "yn": "Run? [y/N] ",
               "typed": "Destructive. Type 'yes' to run: "}
    prompt = prompts[plan.confirm]
    if prompt is not None:
        answer = ask(prompt)
        if answer is None:
            if plan.confirm != "typed" and environ.get("USH_ASSUME_YES"):
                pass
            else:
                ui.line(ui.c("31", "ush: no terminal to confirm; not running "
                                   "(USH_ASSUME_YES=1 allows non-destructive commands)"))
                return EXIT_NO_TTY
        else:
            ok = {"enter": answer.strip().lower() in ("", "y", "yes"),
                  "yn": answer.strip().lower() in ("y", "yes"),
                  "typed": answer.strip() == "yes"}[plan.confirm]
            if not ok:
                ui.line("ush: cancelled")
                return 1

    status = 0
    for idx, step in enumerate(plan.steps):
        if idx and plan.joins[idx - 1] == "&&" and status != 0:
            break
        status = call(step.argv)
    return status


def cmd_exec(args, environ=None, **kw):
    environ = os.environ if environ is None else environ
    if not args:
        sys.stderr.write("usage: ush exec -- COMMAND [ARGS...]\n")
        return 2
    rules = load_rules(find_rules_path(environ))
    host = get_host(rules, environ)
    if host.get("family") is None:
        # Unknown distro: never translate. Run the command as typed if it exists (wrapper
        # path), otherwise behave like "command not found" (handler path).
        if _exists(args[0]):
            return _call(args)
        sys.stderr.write("ush: %s: command not found (host family not supported by ush)\n" % args[0])
        return EXIT_NOT_RUN
    plan = translate(shlex.join(args), host, rules, pick_lang(environ))
    return handle_plan(plan, args, environ, **kw)


def _exists(cmd):
    from shutil import which
    return which(cmd) is not None


def cmd_explain(args, environ=None):
    environ = os.environ if environ is None else environ
    # One argument = a whole command line ("apt install x && y"); several = already split by the
    # user's shell, so keep their boundaries (an empty '' argument must survive).
    text = args[0] if len(args) == 1 else shlex.join(args)
    rules = load_rules(find_rules_path(environ))
    host = get_host(rules, environ)
    plan = translate(text, host, rules, pick_lang(environ))
    out = sys.stdout
    out.write("status:   %s\n" % plan.status)
    if plan.status in ("translated", "passthrough"):
        out.write("command:  %s\n" % to_shell(plan))
    for key in ("fidelity", "risk", "confirm", "reason"):
        val = getattr(plan, key)
        if val and val != "none":
            out.write("%-9s %s\n" % (key + ":", val))
    for n in plan.notes:
        out.write("note:     %s\n" % n)
    return 0


def cmd_hostinfo(_args, environ=None):
    environ = os.environ if environ is None else environ
    rules = load_rules(find_rules_path(environ))
    sys.stdout.write(json.dumps(get_host(rules, environ), sort_keys=True, indent=1) + "\n")
    return 0


def _launcher():
    """How the generated hook should start ush: the checkout's bin/ush when running from a
    source tree, otherwise `python -m babelsh` (pip-installed: there is no bin/ush)."""
    src_bin = os.path.abspath(os.path.join(HERE, "..", "bin", "ush"))
    if os.path.isfile(src_bin):
        return [sys.executable, src_bin]
    return [sys.executable, "-m", "babelsh"]


def cmd_init(args, environ=None):
    environ = os.environ if environ is None else environ
    if len(args) != 1:
        sys.stderr.write("usage: ush init bash|zsh\n")
        return 2
    rules = load_rules(find_rules_path(environ))
    try:
        sys.stdout.write(generate(args[0], rules, _launcher()))
    except ValueError as exc:
        sys.stderr.write("ush: %s\n" % exc)
        return 2
    return 0


def cmd_update(args, environ=None):
    from . import update as upd
    environ = os.environ if environ is None else environ
    dry = "--dry-run" in args
    urls = [a for a in args if not a.startswith("-")]
    unknown = [a for a in args if a.startswith("-") and a != "--dry-run"]
    url = urls[0] if urls else environ.get("USH_RULES_URL")
    if unknown or len(urls) > 1 or not url:
        sys.stderr.write("usage: ush update [--dry-run] [URL]   (or set USH_RULES_URL)\n")
        return 2
    try:
        res = upd.update(url, environ, dry_run=dry)
    except UpdateError as exc:
        sys.stderr.write("ush update: %s\n" % exc)
        return 1
    for w in res.warnings:
        sys.stderr.write("warning: %s\n" % w)
    if res.status == "up_to_date":
        sys.stdout.write("rules are up to date (release %d)\n" % res.installed_version)
        return 0
    verb = "would install" if res.status == "dry_run" else "installed"
    sys.stdout.write("%s release %d (was %s)\n" % (
        verb, res.version, res.installed_version or "none"))
    for line in res.diff:
        sys.stdout.write("  %s\n" % line)
    if res.status == "installed":
        sys.stdout.write("re-run `ush init <shell>` if the release added new commands\n")
    return 0


def cmd_rules(args, environ=None):
    from . import update as upd
    environ = os.environ if environ is None else environ
    state = config_dir(environ)
    if args == ["status"]:
        st = upd.status(state)
        inst = st["installed"]
        if inst is None:
            sys.stdout.write("no downloaded release; using the bundled rules\n")
        else:
            sys.stdout.write("release %s, released %s, expires %s%s\n" % (
                inst["version"], inst["released_at"], inst["expires"],
                "  [STALE: past expiry]" if inst["stale"] else ""))
        sys.stdout.write("version floor: %d (older releases are refused)\n" % st["version_floor"])
        return 0
    if args == ["rollback"]:
        try:
            now_at = upd.rollback(state)
        except UpdateError as exc:
            sys.stderr.write("ush rules: %s\n" % exc)
            return 1
        sys.stdout.write("now using release %s (newer releases stay refused until a newer one "
                         "is published)\n" % now_at)
        return 0
    sys.stderr.write("usage: ush rules status|rollback\n")
    return 2


USAGE = """usage: ush COMMAND
  exec -- CMD [ARGS...]   translate and run one command (used by the shell hook)
  explain CMD...          show what a command would become on this host; runs nothing
  hostinfo                print the detected host profile as JSON
  init bash|zsh           print the shell hook to source from your rc file
  update [--dry-run] [URL]  fetch, verify (minisign) and install a rules release
  rules status|rollback   show the installed release / switch to the previous one
env: USH_DISABLE=1 bypass | USH_DRY_RUN=1 print instead of run | USH_ASSUME_YES=1 | USH_HOST=json | USH_RULES=path
     USH_RULES_URL=update base URL | USH_TRUSTED_KEYS=path to pinned-keys json
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "exec":
        if rest and rest[0] == "--":
            rest = rest[1:]
        return cmd_exec(rest)
    handlers = {"explain": cmd_explain, "hostinfo": cmd_hostinfo, "init": cmd_init,
                "update": cmd_update, "rules": cmd_rules}
    if cmd not in handlers:
        sys.stderr.write(USAGE)
        return 2
    return handlers[cmd](rest)
