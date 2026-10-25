"""Load, validate and index the compiled rules (rules.json). Stdlib only.

Rules are data: this module rejects anything outside the closed vocabulary of SPEC.md.
"""
import json
import re

KNOWN_FAMILIES = ("debian", "fedora", "macos")
FIDELITY_ORDER = {"exact": 0, "approximate": 1, "lossy": 2, "unsupported": 3}
RISK_ORDER = {"read": 0, "modify": 1, "destructive": 2}
ROOT_MODES = ("required", "forbidden", "optional")
ARG_TYPES = ("package_list", "path_list", "string", "service")
LANGS = ("en", "ko")

# Closed list of built-in actions (SPEC §3). Rules reference these by name only.
ACTIONS = {
    "xcode_clt": ["xcode-select", "--install"],
    "noop": None,
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@+._:-]{0,127}$")
ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
INTENT_ID_RE = re.compile(r"^[a-z0-9_.]{1,64}$")
LITERAL_RE = re.compile(r"^[A-Za-z0-9._/@:=+-]*$")
FLAG_RE = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9_-]*$")
MAX_NOTE = 240

_ENTRY_KEYS = ("pkg", "action", "drop", "unsupported")
_FORM_KEYS = {"when", "match", "flag_map", "render", "root", "fidelity", "note", "implicit",
              "unsupported_flags"}
_MATCH_KEYS = {"cmd", "sub", "subsub", "flags_all", "flags_any", "evidence", "takes_value"}
_INTENT_KEYS = {"id", "risk", "args", "flags", "forms", "package_action"}


class RulesError(Exception):
    def __init__(self, errors):
        Exception.__init__(self, "invalid rules:\n  " + "\n  ".join(errors))
        self.errors = errors


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _is_text(v):
    return isinstance(v, str)


def _check_text_map(m, where, errs):
    if not isinstance(m, dict) or not m:
        errs.append("%s: must be a non-empty {lang: text} map" % where)
        return
    for lang, text in m.items():
        if lang not in LANGS:
            errs.append("%s: unknown language %r" % (where, lang))
        elif not _is_text(text) or len(text) > MAX_NOTE:
            errs.append("%s.%s: must be text of at most %d chars" % (where, lang, MAX_NOTE))


def _check_family_notes(m, where, errs):
    if not isinstance(m, dict):
        errs.append("%s: must be a {family: {lang: text}} map" % where)
        return
    for fam, texts in m.items():
        if fam not in KNOWN_FAMILIES:
            errs.append("%s: unknown family %r" % (where, fam))
        else:
            _check_text_map(texts, "%s.%s" % (where, fam), errs)


def _validate_concepts(concepts, errs):
    if not isinstance(concepts, dict):
        errs.append("concepts: must be an object")
        return
    for cid, c in concepts.items():
        where = "concepts.%s" % cid
        if not ID_RE.match(cid):
            errs.append("%s: bad id" % where)
        if not isinstance(c, dict):
            errs.append("%s: must be an object" % where)
            continue
        extra = set(c) - {"kind", "source", "verified", "targets", "aliases", "fidelity", "note"}
        if extra:
            errs.append("%s: unknown keys %s" % (where, sorted(extra)))
        if c.get("kind", "package") != "package":
            errs.append("%s: kind %r not supported in v1 (cask is [later])" % (where, c.get("kind")))
        targets = c.get("targets")
        if not isinstance(targets, dict) or not targets:
            errs.append("%s: targets required" % where)
            targets = {}
        for fam, entries in targets.items():
            w = "%s.targets.%s" % (where, fam)
            if fam not in KNOWN_FAMILIES:
                errs.append("%s: unknown family" % w)
            if not isinstance(entries, list) or not entries:
                errs.append("%s: must be a non-empty list" % w)
                continue
            for e in entries:
                if not isinstance(e, dict):
                    errs.append("%s: entry must be an object" % w)
                    continue
                keys = [k for k in e if k in _ENTRY_KEYS]
                if len(keys) != 1 or len(e) != 1:
                    errs.append("%s: entry needs exactly one of %s (got %s)" % (w, _ENTRY_KEYS, sorted(e)))
                    continue
                k = keys[0]
                if k == "pkg" and not (_is_text(e[k]) and NAME_RE.match(e[k])):
                    errs.append("%s: bad package name %r" % (w, e[k]))
                elif k == "action" and e[k] not in ACTIONS:
                    errs.append("%s: unknown action %r" % (w, e[k]))
                elif k in ("drop", "unsupported") and e[k] is not True:
                    errs.append("%s: %s must be true" % (w, k))
        for fam, names in (c.get("aliases") or {}).items():
            if fam not in KNOWN_FAMILIES:
                errs.append("%s.aliases: unknown family %r" % (where, fam))
            for n in names or []:
                if not (_is_text(n) and NAME_RE.match(n)):
                    errs.append("%s.aliases.%s: bad name %r" % (where, fam, n))
        for fam, fid in (c.get("fidelity") or {}).items():
            if fam not in KNOWN_FAMILIES or fid not in FIDELITY_ORDER or fid == "unsupported":
                errs.append("%s.fidelity: bad entry %r: %r" % (where, fam, fid))
        if "note" in c:
            _check_family_notes(c["note"], where + ".note", errs)


def _validate_services(services, errs):
    if not isinstance(services, dict):
        errs.append("services: must be an object")
        return
    for sid, s in services.items():
        where = "services.%s" % sid
        if not ID_RE.match(sid):
            errs.append("%s: bad id" % where)
        if not isinstance(s, dict):
            errs.append("%s: must be an object" % where)
            continue
        extra = set(s) - {"units", "note", "pkg", "source"}
        if extra:
            errs.append("%s: unknown keys %s" % (where, sorted(extra)))
        units = s.get("units")
        if not isinstance(units, dict) or not units:
            errs.append("%s: units required" % where)
            continue
        for fam, u in units.items():
            if fam not in KNOWN_FAMILIES:
                errs.append("%s.units: unknown family %r" % (where, fam))
            elif u != "!" and not (_is_text(u) and NAME_RE.match(u)):
                errs.append("%s.units.%s: bad unit name %r" % (where, fam, u))
        if "note" in s:
            _check_family_notes(s["note"], where + ".note", errs)


def _check_render(tokens, intent_args, intent_flags, where, errs, used_flags):
    if not isinstance(tokens, list):
        errs.append("%s: must be a list" % where)
        return
    for t in tokens:
        if _is_text(t):
            if not LITERAL_RE.match(t):
                errs.append("%s: literal %r has characters outside the allowed set" % (where, t))
        elif isinstance(t, dict) and len(t) >= 1:
            if "ref" in t and len(t) == 1:
                spec = intent_args.get(t["ref"])
                if spec is None or spec["type"] in ("package_list", "path_list"):
                    errs.append("%s: ref %r must name a scalar argument" % (where, t["ref"]))
            elif "splat" in t and len(t) == 1:
                spec = intent_args.get(t["splat"])
                if spec is None or spec["type"] not in ("package_list", "path_list"):
                    errs.append("%s: splat %r must name a list argument" % (where, t["splat"]))
            elif "if" in t and set(t) <= {"if", "then", "else"}:
                if t["if"] not in intent_flags:
                    errs.append("%s: unknown flag %r in if" % (where, t["if"]))
                else:
                    used_flags.add(t["if"])
                for branch in ("then", "else"):
                    if branch in t:
                        _check_render(t[branch], intent_args, intent_flags, where, errs, used_flags)
            else:
                errs.append("%s: bad template item %r" % (where, t))
        else:
            errs.append("%s: bad template item %r" % (where, t))


def _validate_form(intent_id, intent_args, intent_flags, form, idx, errs):
    where = "intents.%s.forms[%d]" % (intent_id, idx)
    if not isinstance(form, dict):
        errs.append("%s: must be an object" % where)
        return
    extra = set(form) - _FORM_KEYS
    if extra:
        errs.append("%s: unknown keys %s" % (where, sorted(extra)))
    when = form.get("when")
    if not isinstance(when, dict) or not when or set(when) - {"family", "dialect"}:
        errs.append("%s.when: needs family and/or dialect (v1)" % where)
        when = {}
    if "family" in when and when["family"] not in KNOWN_FAMILIES:
        errs.append("%s.when: unknown family %r" % (where, when["family"]))
    if "dialect" in when and not (isinstance(when["dialect"], dict) and when["dialect"]):
        errs.append("%s.when.dialect: must be a non-empty object" % where)
    for name, spec in intent_args.items():
        if spec["type"] in ("package_list", "service") and "family" not in when:
            errs.append("%s: argument %r needs a family-scoped form" % (where, name))
    m = form.get("match")
    known_flags = set()
    if not isinstance(m, dict) or not isinstance(m.get("cmd"), list) or not m["cmd"]:
        errs.append("%s.match.cmd: required list" % where)
    else:
        extra = set(m) - _MATCH_KEYS
        if extra:
            errs.append("%s.match: unknown keys %s" % (where, sorted(extra)))
        for c in m["cmd"]:
            if not (_is_text(c) and NAME_RE.match(c)):
                errs.append("%s.match.cmd: bad command %r" % (where, c))
        for key in ("sub", "subsub"):
            if key in m and not (_is_text(m[key]) and NAME_RE.match(m[key])):
                errs.append("%s.match.%s: bad value" % (where, key))
        for key in ("flags_all", "flags_any", "takes_value"):
            for f in m.get(key, []):
                if not (_is_text(f) and FLAG_RE.match(f)):
                    errs.append("%s.match.%s: bad flag %r" % (where, key, f))
                known_flags.add(f)
        for ev in m.get("evidence", []):
            if not (isinstance(ev, dict) and set(ev) == {"after_flag", "token"}
                    and _is_text(ev["after_flag"]) and _is_text(ev["token"])):
                errs.append("%s.match.evidence: items need after_flag and token" % where)
    flag_map = form.get("flag_map", {})
    if not isinstance(flag_map, dict):
        errs.append("%s.flag_map: must be an object" % where)
        flag_map = {}
    for f, canon in flag_map.items():
        if not FLAG_RE.match(f):
            errs.append("%s.flag_map: bad flag %r" % (where, f))
        if canon not in intent_flags:
            errs.append("%s.flag_map: %r maps to unknown canonical flag %r" % (where, f, canon))
    if form.get("root", "optional") not in ROOT_MODES:
        errs.append("%s.root: must be one of %s" % (where, ROOT_MODES))
    if form.get("fidelity", "exact") not in FIDELITY_ORDER:
        errs.append("%s.fidelity: bad value" % where)
    if "note" in form:
        _check_text_map(form["note"], where + ".note", errs)
    used = set()
    _check_render(form.get("render"), intent_args, intent_flags, where + ".render", errs, used)
    implicit = set(form.get("implicit", []))
    unsupported = set(form.get("unsupported_flags", []))
    for canon in sorted(set(intent_flags)):
        handled = (canon in used) + (canon in implicit) + (canon in unsupported)
        if handled != 1:
            errs.append("%s: canonical flag %r must be exactly one of rendered/implicit/"
                        "unsupported_flags (SPEC 4.3.1), found %d" % (where, canon, handled))
    for canon in implicit | unsupported:
        if canon not in intent_flags:
            errs.append("%s: %r is not a canonical flag of this intent" % (where, canon))


def _validate_intents(intents, errs):
    if not isinstance(intents, list):
        errs.append("intents: must be a list")
        return
    seen = set()
    for intent in intents:
        iid = intent.get("id") if isinstance(intent, dict) else None
        if not (_is_text(iid) and INTENT_ID_RE.match(iid)):
            errs.append("intents: bad id %r" % (iid,))
            continue
        if iid in seen:
            errs.append("intents: duplicate id %r" % iid)
        seen.add(iid)
        extra = set(intent) - _INTENT_KEYS
        if extra:
            errs.append("intents.%s: unknown keys %s" % (iid, sorted(extra)))
        if intent.get("risk") not in RISK_ORDER:
            errs.append("intents.%s.risk: required, one of %s" % (iid, sorted(RISK_ORDER)))
        if intent.get("package_action", "none") not in ("none", "install"):
            errs.append("intents.%s.package_action: none|install" % iid)
        args = intent.get("args", {})
        for name, spec in args.items():
            if not ID_RE.match(name) or not isinstance(spec, dict) or spec.get("type") not in ARG_TYPES \
                    or set(spec) - {"type", "min", "optional"}:
                errs.append("intents.%s.args.%s: bad spec" % (iid, name))
                args = {}
                break
        flags = intent.get("flags", {})
        for name, spec in flags.items():
            if not ID_RE.match(name) or spec != {"type": "bool"}:
                errs.append("intents.%s.flags.%s: only {type: bool} supported" % (iid, name))
        forms = intent.get("forms")
        if not isinstance(forms, list) or not forms:
            errs.append("intents.%s.forms: required" % iid)
            continue
        for idx, form in enumerate(forms):
            _validate_form(iid, args, flags, form, idx, errs)


def validate(data):
    """Return a list of error strings (empty if the rules are acceptable)."""
    if not isinstance(data, dict):
        return ["rules must be an object"]
    errs = []
    if data.get("schema") != 1:
        errs.append("schema must be 1")
    extra = set(data) - {"schema", "concepts", "services", "intents"}
    if extra:
        errs.append("unknown top-level keys %s" % sorted(extra))
    _validate_concepts(data.get("concepts", {}), errs)
    _validate_services(data.get("services", {}), errs)
    _validate_intents(data.get("intents", []), errs)
    return errs


# ---------------------------------------------------------------------------
# indexes
# ---------------------------------------------------------------------------

def _add_index(index, fam, name, cid, kind, errs):
    bucket = index.setdefault(fam, {})
    if bucket.get(name, cid) != cid:
        errs.append("%s name %r (%s) maps to both %r and %r" % (kind, name, fam, bucket[name], cid))
    bucket[name] = cid


def build_indexes(concepts, services):
    """(family, name) -> concept id. Multi-package targets are not reverse-indexed (SPEC §3)."""
    errs = []
    pkg = {}
    for cid, c in concepts.items():
        for fam, entries in c["targets"].items():
            if len(entries) == 1 and "pkg" in entries[0]:
                _add_index(pkg, fam, entries[0]["pkg"], cid, "package", errs)
        for fam, names in (c.get("aliases") or {}).items():
            for n in names or []:
                _add_index(pkg, fam, n, cid, "package", errs)
    svc = {}
    for sid, s in services.items():
        for fam, unit in s["units"].items():
            if unit != "!":
                _add_index(svc, fam, unit, sid, "service", errs)
    return pkg, svc, errs


class Rules(object):
    def __init__(self, data):
        errs = validate(data)
        if errs:
            raise RulesError(errs)
        self.data = data
        self.concepts = data.get("concepts", {})
        self.services = data.get("services", {})
        self.intents = data.get("intents", [])
        self.pkg_index, self.svc_index, ierrs = build_indexes(self.concepts, self.services)
        if ierrs:
            raise RulesError(ierrs)
        self.forms = []
        self.by_cmd = {}
        self.cmds = set()
        self.dialect_cmds = set()
        for intent in self.intents:
            for form in intent["forms"]:
                self.forms.append((intent, form))
                for c in form["match"]["cmd"]:
                    self.cmds.add(c)
                    self.by_cmd.setdefault(c, []).append((intent, form))
                    if "dialect" in form["when"]:
                        self.dialect_cmds.add(c)


def load_rules(path):
    with open(path, "r", encoding="utf-8") as fh:
        return Rules(json.load(fh))
