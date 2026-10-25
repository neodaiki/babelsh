#!/usr/bin/env python3
"""Record the README demo as an asciicast (v2) from a *real* interactive bash session.

What is real: babelsh is installed from PyPI (or a path) into a throwaway venv, its hook is
sourced by `bash -i` running in a pseudo-terminal, and every line of output, including the
confirmation prompts read from /dev/tty, is produced by that session. What is scripted: the
keystrokes and their timing. What is simulated (and labelled on screen): the macOS segment,
which uses a pretend host profile.

  python3 tools/record_demo.py --out docs/demo.cast [--package babelsh | /path/to/checkout]

Needs `pexpect` and network access (for the PyPI install). Run on a Linux machine whose
`sudo`/`apt` you are happy to leave untouched: every install prompt is answered "n".
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

import pexpect

COLS, ROWS = 92, 26
MAC_PROFILE = {"family": "macos", "os_id": "macos-14", "arch": "arm64",
               "dialect": {"sed": "bsd"}, "has": {"brew": True, "sudo": True, "sed": True},
               "is_root": False}


class Cast(object):
    """pexpect `logfile_read` sink that writes asciicast v2 events with real timing."""

    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        header = {"version": 2, "width": COLS, "height": ROWS, "timestamp": int(time.time()),
                  "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
                  "title": "babelsh: type the commands you know, run them where you are"}
        self.f.write(json.dumps(header) + "\n")
        self.t0 = None
        self.on = False

    def write(self, data):
        if not self.on:
            return
        now = time.monotonic()
        if self.t0 is None:
            self.t0 = now
        self.f.write(json.dumps([round(now - self.t0, 6), "o", data], ensure_ascii=False) + "\n")

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.close()


def pause(seconds):
    time.sleep(seconds)


def type_text(child, text, fast=False):
    for ch in text:
        child.send(ch)
        time.sleep(random.uniform(0.008, 0.02) if fast else random.uniform(0.022, 0.055))


def at_prompt(child, timeout=60):
    try:
        child.expect(r"\r\n\$ $", timeout=timeout)
    except pexpect.TIMEOUT:
        sys.stderr.write("timed out waiting for the prompt; screen so far: %r\n" % (child.before + child.buffer)[-400:])
        raise


def run(child, line, think=0.5, fast=False):
    """Type a command line like a person would, press Enter, wait for the next prompt."""
    type_text(child, line, fast)
    pause(think)
    child.send("\r")
    at_prompt(child)
    pause(0.9)


def run_with_confirmation(child, line, answer, read_pause=1.6):
    type_text(child, line)
    pause(0.5)
    child.send("\r")
    child.expect_exact("] ", timeout=60)       # the "Run? [y/N] " prompt
    pause(read_pause)                           # let the viewer read the translation
    type_text(child, answer)
    pause(0.4)
    child.send("\r")
    at_prompt(child)
    pause(0.9)


def comment(child, text):
    run(child, "# " + text, think=0.15, fast=True)
    pause(0.25)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", default="docs/demo.cast")
    ap.add_argument("--package", default="babelsh", help="PyPI name or path to install")
    ap.add_argument("--seed", type=int, default=7, help="typing-speed jitter seed")
    args = ap.parse_args()
    random.seed(args.seed)

    work = tempfile.mkdtemp(prefix="babelsh-demo-")
    try:
        venv = os.path.join(work, "venv")
        home = os.path.join(work, "home")
        cwd = os.path.join(work, "project")
        for d in (home, cwd):
            os.makedirs(d)
        subprocess.check_call([sys.executable, "-m", "venv", venv])
        subprocess.check_call([os.path.join(venv, "bin", "pip"), "install", "-q", args.package])
        version = subprocess.check_output([os.path.join(venv, "bin", "pip"), "show", "babelsh"],
                                          universal_newlines=True)
        print([l for l in version.splitlines() if l.startswith("Version")][0], file=sys.stderr)

        hook_dir = os.path.join(home, ".config", "ush")
        os.makedirs(hook_dir)
        hook = os.path.join(hook_dir, "hook.bash")
        with open(hook, "w") as fh:
            fh.write(subprocess.check_output([os.path.join(venv, "bin", "ush"), "init", "bash"],
                                             universal_newlines=True, env=dict(os.environ, HOME=home)))

        inputrc = os.path.join(work, "inputrc")     # no bracketed-paste escapes in the recording
        with open(inputrc, "w") as fh:
            fh.write("set enable-bracketed-paste off\n")
        env = {"INPUTRC": inputrc, "PATH": os.path.join(venv, "bin") + ":/usr/local/bin:/usr/bin:/bin",
               "HOME": home, "TERM": "xterm-256color", "LANG": "C.UTF-8", "PS1": "$ ",
               "COLUMNS": str(COLS), "LINES": str(ROWS)}
        cast = Cast(args.out)
        child = pexpect.spawn("/bin/bash", ["--norc", "--noprofile", "-i"], env=env, cwd=cwd,
                              encoding="utf-8", dimensions=(ROWS, COLS), timeout=60)
        child.delaybeforesend = None           # pexpect sleeps 50 ms before every send otherwise
        child.logfile_read = cast
        child.expect(r"[$#] ")
        child.send("PS1='$ '\r")               # bash ignores an inherited PS1 under --norc
        child.expect(r"\r\n\$ $")

        # --- off camera: load the hook, define the "pretend Mac" helper, clear the screen
        child.send("source %s\r" % hook)
        at_prompt(child)
        child.send("mac() { USH_HOST='%s' \"$@\"; }\r" % json.dumps(MAC_PROFILE))
        at_prompt(child)
        child.send("clear\r")
        child.expect(r"\x1b\[3J\$ $")            # the prompt follows the clear sequence directly
        cast.on = True
        cast.write("\x1b[2J\x1b[H$ ")

        # --- on camera
        source = "PyPI" if args.package == "babelsh" else "a local checkout"
        comment(child, "babelsh is installed from %s and hooked into bash. This machine: Ubuntu." % source)
        comment(child, "Fedora habit:")
        run_with_confirmation(child, "dnf install -y htop", "n")
        comment(child, "Homebrew habit:")
        run_with_confirmation(child, "brew install htop", "n")
        comment(child, "macOS-style sed on GNU sed:")
        run(child, "echo 'hello foo' > app.conf", think=0.2)
        run_with_confirmation(child, "sed -i '' 's/foo/bar/' app.conf", "y")
        run(child, "cat app.conf", think=0.2)
        pause(0.6)
        comment(child, "Pretending to be a Mac (SIMULATED host profile; not a real Mac):")
        run(child, "mac ush explain 'sudo apt install -y nginx'", think=0.3)
        pause(1.2)
        run(child, "mac ush explain 'systemctl start ssh'", think=0.3)
        pause(1.8)
        comment(child, "Anything else is left alone:")
        run(child, "gti status", think=0.3)
        pause(0.8)
        comment(child, "alpha. Ubuntu verified; macOS plans checked in CI only. Need a Mac tester!")
        pause(2.5)

        cast.on = False                         # don't record the shell exiting
        cast.flush()
        child.send("exit\r")
        child.expect(pexpect.EOF, timeout=20)
        cast.close()
        print("wrote %s" % args.out, file=sys.stderr)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
