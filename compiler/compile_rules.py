#!/usr/bin/env python3
"""Compile the YAML rules (packages/services/commands) into canonical rules.json.

Repo-side tool: may use PyYAML. The client never reads YAML.
Usage: compile_rules.py RULES_DIR [-o OUT.json]
"""
import argparse
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ush.rules import KNOWN_FAMILIES, Rules, RulesError  # noqa: E402


class CompileError(Exception):
    pass


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _short_targets(val, where):
    """Shorthand value for one family -> canonical target entry list (SPEC §3.0)."""
    if val is None:
        return [{"drop": True}]
    if val == "!":
        return [{"unsupported": True}]
    if isinstance(val, str):
        return [{"pkg": val}]
    if isinstance(val, dict):
        return [val]
    if isinstance(val, list):
        return [{"pkg": x} if isinstance(x, str) else x for x in val]
    raise CompileError("%s: cannot interpret %r" % (where, val))


def compile_packages(doc):
    defaults = doc.get("defaults") or {}
    concepts = {}
    for cid, fams in (doc.get("packages") or {}).items():
        unknown = set(fams) - set(KNOWN_FAMILIES)
        if unknown:
            raise CompileError("packages.%s: unknown families %s" % (cid, sorted(unknown)))
        c = {"kind": "package",
             "targets": {f: _short_targets(v, "packages.%s.%s" % (cid, f)) for f, v in fams.items()}}
        if "source" in defaults:
            c["source"] = defaults["source"]
        concepts[cid] = c
    for long in doc.get("concepts") or []:
        long = dict(long)
        cid = long.pop("id")
        base = concepts.setdefault(cid, {"kind": "package"})
        if "source" in defaults:
            base.setdefault("source", defaults["source"])
        base.update(long)
    return concepts


def compile_services(doc):
    defaults = doc.get("defaults") or {}
    out = {}
    for sid, body in (doc.get("services") or {}).items():
        body = dict(body)
        units = {f: body.pop(f) for f in list(body) if f in KNOWN_FAMILIES}
        s = {"units": units}
        for key in ("note", "pkg"):
            if key in body:
                s[key] = body.pop(key)
        if body:
            raise CompileError("services.%s: unknown keys %s" % (sid, sorted(body)))
        if "source" in defaults:
            s["source"] = defaults["source"]
        out[sid] = s
    return out


def compile_dir(rules_dir):
    data = {"schema": 1, "concepts": {}, "services": {}, "intents": []}
    path = os.path.join(rules_dir, "packages.yaml")
    if os.path.exists(path):
        data["concepts"] = compile_packages(_load(path))
    path = os.path.join(rules_dir, "services.yaml")
    if os.path.exists(path):
        data["services"] = compile_services(_load(path))
    path = os.path.join(rules_dir, "commands.yaml")
    if os.path.exists(path):
        data["intents"] = _load(path).get("intents") or []
    Rules(data)  # validates schema, flag coverage and reverse-index uniqueness
    return data


def dumps(data):
    """Serialize rules.json. Key order is semantic (an intent's `args` are assigned to
    positionals in declaration order), so keys must NOT be sorted."""
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("rules_dir")
    ap.add_argument("-o", "--out", default="build/rules.json")
    args = ap.parse_args(argv)
    try:
        data = compile_dir(args.rules_dir)
    except (CompileError, RulesError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(dumps(data))
    print("wrote %s (%d concepts, %d services, %d intents)" % (
        args.out, len(data["concepts"]), len(data["services"]), len(data["intents"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
