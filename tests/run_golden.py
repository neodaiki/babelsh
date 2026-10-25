#!/usr/bin/env python3
"""Run spec/tests/golden.yaml against the compiled example rules.

  python3 tests/run_golden.py                       # run all cases
  python3 tests/run_golden.py -k sed                # only cases whose id contains "sed"
  python3 tests/run_golden.py --explain macos14 "apt install -y nginx"
"""
import argparse
import os
import sys

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "compiler"))
import json  # noqa: E402

from compile_rules import compile_dir, dumps  # noqa: E402
from babelsh import Rules, to_shell, translate  # noqa: E402

SPEC = os.path.join(ROOT, "spec")


def load_world():
    # Round-trip through the exact serialization the client will read, so a serialization
    # bug (e.g. key order) cannot hide behind in-memory data.
    rules = Rules(json.loads(dumps(compile_dir(os.path.join(SPEC, "examples")))))
    with open(os.path.join(SPEC, "tests", "hosts.yaml"), encoding="utf-8") as fh:
        hosts = yaml.safe_load(fh)
    return rules, hosts


def check(case, rules, hosts):
    """Return a list of mismatch descriptions (empty = pass)."""
    exp = case["expect"]
    plan = translate(case["input"], hosts[case["host"]], rules)
    bad = []

    def cmp(name, got, want):
        if got != want:
            bad.append("%s: got %r, want %r" % (name, got, want))

    cmp("status", plan.status, exp["status"])
    if "steps" in exp:
        cmp("steps", [{"argv": s.argv} for s in plan.steps], exp["steps"])
        cmp("joins", plan.joins, exp.get("joins", []))
    for key in ("fidelity", "risk", "confirm", "reason"):
        if key in exp:
            cmp(key, getattr(plan, key), exp[key])
    if "command" in exp:
        cmp("command", to_shell(plan), exp["command"])
    blob = " | ".join(plan.notes)
    for sub in exp.get("notes", []):
        if sub not in blob:
            bad.append("notes: %r not found in %r" % (sub, plan.notes))
    for sub in exp.get("notes_absent", []):
        if sub in blob:
            bad.append("notes_absent: %r found in %r" % (sub, plan.notes))
    return bad, plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="substring filter on case id")
    ap.add_argument("--explain", nargs=2, metavar=("HOST", "COMMAND"))
    ap.add_argument("--lang", default="en")
    args = ap.parse_args()
    rules, hosts = load_world()

    if args.explain:
        host, command = args.explain
        plan = translate(command, hosts[host], rules, args.lang)
        print("status:   %s" % plan.status)
        if plan.status in ("translated", "passthrough"):
            print("command:  %s" % to_shell(plan))
        for key in ("fidelity", "risk", "confirm", "reason"):
            if getattr(plan, key):
                print("%-9s %s" % (key + ":", getattr(plan, key)))
        for n in plan.notes:
            print("note:     %s" % n)
        return 0

    with open(os.path.join(SPEC, "tests", "golden.yaml"), encoding="utf-8") as fh:
        cases = [c for c in yaml.safe_load(fh)["cases"] if args.k in c["id"]]
    failed = 0
    for case in cases:
        bad, plan = check(case, rules, hosts)
        if bad:
            failed += 1
            print("FAIL %s   [%s] %s" % (case["id"], case["host"], case["input"]))
            for b in bad:
                print("       " + b)
        else:
            print("ok   %s" % case["id"])
    print("\n%d cases, %d failed" % (len(cases), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
