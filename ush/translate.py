"""translate(text, host, rules) -> Plan. Pure: no I/O, no execution (SPEC §4.7).

Pipeline per segment: sudo strip -> parse against every form of the command -> resolve names
through the source family -> disambiguate -> render with the host's form.
"""
import re

from .plan import Plan, Step
from .rules import ACTIONS, FIDELITY_ORDER, RISK_ORDER
from .shellparse import ShellParseError, split

MESSAGES = {
    "en": {
        "sudo_removed": "sudo removed: Homebrew must not run as root.",
        "no_sudo": "root privileges are required but sudo is not available.",
        "unknown_name": "no mapping for '{name}'",
        "no_family_target": "'{name}' has no equivalent on {family}",
        "unmapped_flag": "flag '{name}' has no safe translation",
        "no_rule": "no translation rule for '{name}'",
        "bad_arguments": "unexpected arguments for '{name}'",
        "dynamic_argument": "argument '{name}' is computed by the shell; cannot translate it safely",
        "shell_syntax": "'{name}' uses shell syntax (redirect/background/subshell/assignment) ush does not translate",
        "ambiguous": "'{name}' is ambiguous between dialects; run it natively or be explicit",
        "no_host_form": "no rule for this operation on {family}",
        "backup_suffix": "backup suffix {name} is not supported yet",
        "pipeline_multi": "a translated step expands to several commands inside a pipeline",
        "action_skipped": "install-only step for '{name}' skipped",
        "parse_error": "could not parse the command line ({name}); leaving it to the shell",
        "nothing": "nothing to do on this system",
        "redirect_multi": "a redirect cannot be applied to a command that expands to several steps",
    },
    "ko": {
        "sudo_removed": "sudo 제거: Homebrew는 root로 실행하면 안 됩니다.",
        "no_sudo": "root 권한이 필요하지만 sudo를 사용할 수 없습니다.",
        "unknown_name": "'{name}'에 대한 매핑이 없습니다",
        "no_family_target": "'{name}'은(는) {family}에 대응 항목이 없습니다",
        "unmapped_flag": "플래그 '{name}'은(는) 안전하게 번역할 수 없습니다",
        "no_rule": "'{name}'에 대한 번역 규칙이 없습니다",
        "bad_arguments": "'{name}'의 인자가 올바르지 않습니다",
        "dynamic_argument": "인자 '{name}'은(는) 셸이 계산하므로 안전하게 번역할 수 없습니다",
        "shell_syntax": "'{name}'에 ush가 번역하지 않는 셸 문법(리다이렉트/백그라운드/서브셸/대입)이 있습니다",
        "ambiguous": "'{name}'은(는) 방언 간 구분이 모호합니다. 직접 지정해서 실행하세요",
        "no_host_form": "{family}에는 이 작업에 대한 규칙이 없습니다",
        "backup_suffix": "백업 접미사 {name}은(는) 아직 지원하지 않습니다",
        "pipeline_multi": "번역된 단계가 파이프라인 안에서 여러 명령으로 확장됩니다",
        "action_skipped": "'{name}'의 설치 전용 단계를 건너뜁니다",
        "parse_error": "명령줄을 해석할 수 없습니다({name}). 셸에 맡깁니다",
        "nothing": "이 시스템에서는 할 일이 없습니다",
        "redirect_multi": "여러 단계로 확장되는 명령에는 리다이렉트를 적용할 수 없습니다",
    },
}

_CLUSTER = re.compile(r"^-[A-Za-z]{2,}$")
_SUDO_PREFIX = re.compile(r"^\s*sudo\s+")


class _Host(object):
    def __init__(self, d):
        self.family = d["family"]
        self.dialect = d.get("dialect") or {}
        self.has = d.get("has") or {}
        self.is_root = bool(d.get("is_root"))

    def matches(self, when):
        if not when:
            return False
        if "family" in when and when["family"] != self.family:
            return False
        for k, v in (when.get("dialect") or {}).items():
            if self.dialect.get(k) != v:
                return False
        return True


class _Parsed(object):
    __slots__ = ("status", "detail", "flags", "flag_values", "values")

    def __init__(self, status, detail=None):
        self.status = status          # ok | nomatch | unmapped | badargs
        self.detail = detail or []
        self.flags = set()            # canonical flags
        self.flag_values = {}         # takes_value flags -> value
        self.values = {}              # arg name -> str | list


class _Seg(object):
    """Result for one segment."""
    __slots__ = ("kind", "steps", "inner", "fidelity", "risk", "notes", "reason")

    def __init__(self, kind, steps=None, inner=None, fidelity="exact", risk=None, notes=None,
                 reason=None):
        self.kind = kind              # pass | fixed | translated | unsupported | refused
        self.steps = steps or []
        self.inner = inner or []      # operators between this segment's own steps
        self.fidelity = fidelity
        self.risk = risk
        self.notes = notes or []
        self.reason = reason


def _worst(order, a, b):
    return a if order[a] >= order[b] else b


class Translator(object):
    def __init__(self, rules, host, lang="en"):
        self.rules = rules
        self.host = _Host(host)
        self.msg = MESSAGES.get(lang, MESSAGES["en"])
        self.lang = lang if lang in MESSAGES else "en"
        native = set()
        for _intent, form in rules.forms:
            if self.host.matches(form["when"]):
                native.update(form["match"]["cmd"])
        for c in rules.cmds:
            if self.host.has.get(c):
                native.add(c)
        # dialect-sensitive commands exist everywhere but are not "native" in the
        # sense of being left alone: their flags differ per host.
        self.native = native - rules.dialect_cmds

    # -- helpers -------------------------------------------------------
    def _m(self, key, **kw):
        return self.msg[key].format(**kw)

    def _text(self, texts):
        return texts.get(self.lang) or texts.get("en") or ""

    def _pass(self, seg):
        return _Seg("pass", [Step([t.value for t in seg.tokens], seg.raw)])

    def _host_form(self, intent):
        for form in intent["forms"]:
            if self.host.matches(form["when"]):
                return form
        return None

    # -- entry ---------------------------------------------------------
    def translate(self, text):
        try:
            segments = split(text)
        except ShellParseError as exc:
            return Plan("passthrough", text, notes=[self._m("parse_error", name=str(exc))])
        if not segments:
            return Plan("passthrough", text)
        results = [self._segment(s) for s in segments]

        refused = [r for r in results if r.kind == "refused"]
        if refused:
            first = refused[0]
            return Plan("refused", text, notes=_dedupe(n for r in refused for n in r.notes),
                        reason=first.reason)
        unsupported = [r for r in results if r.kind == "unsupported"]
        if unsupported:
            risk = None
            for r in unsupported:
                risk = r.risk if risk is None else _worst(RISK_ORDER, risk, r.risk)
            return Plan("unsupported", text, fidelity="unsupported", risk=risk,
                        notes=_dedupe(n for r in results for n in r.notes))
        changed = [r for r in results if r.kind in ("fixed", "translated")]
        if not changed:
            return Plan("passthrough", text, notes=_dedupe(n for r in results for n in r.notes))

        steps, joins = [], []
        for idx, (seg, res) in enumerate(zip(segments, results)):
            prev_op = segments[idx - 1].op if idx else None
            if len(res.steps) > 1 and (seg.op == "|" or prev_op == "|"):
                return Plan("refused", text, notes=[self._m("pipeline_multi")], reason="shell_syntax")
            if idx:
                joins.append(prev_op)
            for k, step in enumerate(res.steps):
                if k:
                    joins.append(res.inner[k - 1])
                steps.append(step)
        fidelity, risk = "exact", None
        for r in changed:
            fidelity = _worst(FIDELITY_ORDER, fidelity, r.fidelity)
            if r.risk is not None:
                risk = r.risk if risk is None else _worst(RISK_ORDER, risk, r.risk)
        notes = _dedupe(n for r in results for n in r.notes)
        if not steps:
            return Plan("noop", text, notes=notes + [self._m("nothing")])
        return Plan("translated", text, steps, joins, fidelity, risk,
                    _confirm(risk, steps), notes)

    # -- one segment ---------------------------------------------------
    def _segment(self, seg):
        toks = seg.tokens
        sudo = False
        argv_toks = toks
        if toks[0].value == "sudo":
            if len(toks) == 1 or toks[1].value.startswith("-"):
                return self._pass(seg)          # sudo options are not modelled in v1
            sudo, argv_toks = True, toks[1:]
        cmd = argv_toks[0].value
        if cmd not in self.rules.cmds:
            return self._pass(seg)
        native = cmd in self.native
        exists_here = native or cmd in self.rules.dialect_cmds
        clean = not seg.syntax and not any(t.dynamic for t in argv_toks)

        if native:
            if sudo and clean:
                fixed = self._fixup(seg, argv_toks)
                if fixed is not None:
                    return fixed
            return self._pass(seg)
        if not clean:
            if exists_here:
                return self._pass(seg)
            if seg.syntax:
                return self._refuse("shell_syntax", cmd)
            dyn = [t.value for t in argv_toks if t.dynamic][0]
            return self._refuse("dynamic_argument", dyn)

        oks, failures = [], []
        for intent, form in self.rules.by_cmd[cmd]:
            pr = self._parse_form(intent, form, argv_toks)
            (oks if pr.status == "ok" else failures).append((intent, form, pr))
        if not oks:
            return self._parse_failure(seg, cmd, argv_toks, failures, exists_here)

        resolved, missing = [], []
        for intent, form, pr in oks:
            res, miss = self._resolve(intent, form, pr)
            if miss:
                missing.extend(miss)
            else:
                resolved.append((intent, form, pr, res))
        if not resolved:
            if exists_here:
                return self._pass(seg)
            return self._refuse("unknown_name", *_dedupe(missing))

        groups = {}
        for cand in resolved:
            groups.setdefault(_signature(*cand), []).append(cand)
        glist = list(groups.values())
        values = [t.value for t in argv_toks]
        if len(glist) == 1:
            chosen = glist[0]
        else:
            with_ev = [g for g in glist if any(_has_evidence(c[1], values) for c in g)]
            if len(with_ev) == 1:
                chosen = with_ev[0]
            else:
                with_host = [g for g in glist
                             if any(c[1] is self._host_form(c[0]) for c in g)]
                if len(with_host) == 1:
                    chosen = with_host[0]
                else:
                    return self._refuse("ambiguous", cmd)

        intent = chosen[0][0]
        hform = self._host_form(intent)
        if any(c[1] is hform for c in chosen):
            return self._pass(seg)              # already the host's dialect
        if hform is None:
            return self._refuse("no_host_form", family=self.host.family)
        _, _, pr, res = chosen[0]
        return self._with_redirs(seg, self._render(intent, hform, pr, res, sudo))

    def _with_redirs(self, seg, res):
        """Carry simple redirects (`> log 2>&1`) over to a translated single command."""
        if res.kind != "translated" or not seg.redirs or not res.steps:
            return res
        if len(res.steps) > 1:
            return self._refuse("redirect_multi", "")
        res.steps[0].redir = " ".join(seg.redirs)
        return res

    def _refuse(self, reason, *names, **kw):
        notes = []
        if reason == "unknown_name":
            notes = [self._m("unknown_name", name=n) for n in names]
        elif reason == "no_host_form":
            notes = [self._m("no_host_form", **kw)]
        elif reason == "redirect_multi":
            notes = [self._m("redirect_multi")]
            reason = "shell_syntax"
        else:
            notes = [self._m(reason, name=names[0] if names else "")]
        return _Seg("refused", notes=notes, reason=reason)

    def _parse_failure(self, seg, cmd, argv_toks, failures, exists_here):
        if exists_here:
            return self._pass(seg)              # never block a command the host already has
        unmapped = [d for _i, _f, pr in failures if pr.status == "unmapped" for d in pr.detail]
        if unmapped:
            return _Seg("refused", notes=[self._m("unmapped_flag", name=n) for n in _dedupe(unmapped)],
                        reason="unmapped_flag")
        if any(pr.status == "badargs" for _i, _f, pr in failures):
            return self._refuse("bad_arguments", cmd)
        shown = " ".join(t.value for t in argv_toks[:2])
        return self._refuse("no_rule", shown)

    def _fixup(self, seg, argv_toks):
        """Native command typed with sudo where the tool forbids root (sudo brew install)."""
        for intent, form in self.rules.by_cmd[argv_toks[0].value]:
            if form.get("root") != "forbidden":
                continue
            if self._parse_form(intent, form, argv_toks).status == "ok":
                step = Step([t.value for t in argv_toks], _SUDO_PREFIX.sub("", seg.raw, count=1))
                return _Seg("fixed", [step], risk=intent["risk"],
                            notes=[self._m("sudo_removed")])
        return None

    # -- parsing against one form -------------------------------------
    def _parse_form(self, intent, form, argv_toks):
        m = form["match"]
        argv = [t.value for t in argv_toks]
        if argv[0] not in m["cmd"]:
            return _Parsed("nomatch")
        rest = argv[1:]
        pos = 0
        for key in ("sub", "subsub"):
            if key in m:
                if pos >= len(rest) or rest[pos] != m[key]:
                    return _Parsed("nomatch")
                pos += 1
        rest = rest[pos:]

        flag_map = form.get("flag_map", {})
        takes_value = set(m.get("takes_value", []))
        known = set(flag_map) | set(m.get("flags_all", [])) | set(m.get("flags_any", [])) | takes_value
        seen, values, positionals, unmapped = set(), {}, [], []
        i, after_dd = 0, False
        while i < len(rest):
            tok = rest[i]
            if after_dd or not tok.startswith("-") or tok == "-":
                positionals.append(tok)
                i += 1
                continue
            if tok == "--":
                after_dd = True
                i += 1
                continue
            expanded = [tok]
            if tok not in known and _CLUSTER.match(tok):
                letters = ["-" + ch for ch in tok[1:]]
                if all(f in known and f not in takes_value for f in letters):
                    expanded = letters
            for f in expanded:
                if f not in known:
                    unmapped.append(tok)
                    continue
                seen.add(f)
                if f in takes_value:
                    if i + 1 >= len(rest):
                        return _Parsed("badargs", [f])
                    values[f] = rest[i + 1]
                    i += 1
            i += 1

        if not set(m.get("flags_all", [])) <= seen:
            return _Parsed("nomatch")
        if m.get("flags_any") and not (set(m["flags_any"]) & seen):
            return _Parsed("nomatch")
        if unmapped:
            return _Parsed("unmapped", unmapped)

        pr = _Parsed("ok")
        pr.flags = {flag_map[f] for f in seen if f in flag_map}
        pr.flag_values = values
        pos_left = list(positionals)
        for name, spec in intent.get("args", {}).items():
            t = spec["type"]
            if t in ("package_list", "path_list"):
                if len(pos_left) < spec.get("min", 0):
                    return _Parsed("badargs", [name])
                pr.values[name] = pos_left
                pos_left = []
            else:
                if not pos_left:
                    if spec.get("optional"):
                        continue
                    return _Parsed("badargs", [name])
                pr.values[name] = pos_left.pop(0)
        if pos_left:
            return _Parsed("badargs", pos_left)
        return pr

    # -- name resolution -----------------------------------------------
    def _resolve(self, intent, form, pr):
        fam = form["when"].get("family")
        res, missing = {}, []
        for name, spec in intent.get("args", {}).items():
            v = pr.values.get(name)
            if v is None:
                res[name] = None
            elif spec["type"] == "package_list":
                ids = []
                for n in v:
                    cid = self.rules.pkg_index.get(fam, {}).get(n)
                    if cid is None:
                        missing.append(n)
                    else:
                        ids.append(cid)
                res[name] = tuple(ids)
            elif spec["type"] == "service":
                cid = self.rules.svc_index.get(fam, {}).get(v)
                if cid is None:
                    missing.append(v)
                res[name] = cid
            else:
                res[name] = tuple(v) if isinstance(v, list) else v
        return res, missing

    # -- rendering -----------------------------------------------------
    def _render(self, intent, hform, pr, res, had_sudo):
        fam = self.host.family
        for flag, value in pr.flag_values.items():
            if value != "":
                return self._refuse("backup_suffix", "%s %s" % (flag, value))
        notes = []
        fidelity = hform.get("fidelity", "exact")
        argvals, extras, unsupported = {}, [], False
        skip_main = False
        for name, spec in intent.get("args", {}).items():
            t = spec["type"]
            v = res.get(name)
            if v is None:
                continue
            if t == "package_list":
                names = []
                for cid in v:
                    concept = self.rules.concepts[cid]
                    entries = concept["targets"].get(fam)
                    if entries is None:
                        return _Seg("refused", notes=[self._m("no_family_target", name=cid,
                                                              family=fam)], reason="unknown_name")
                    before = (len(names), len(extras))
                    for e in entries:
                        if "pkg" in e:
                            if e["pkg"] not in names:
                                names.append(e["pkg"])
                        elif "action" in e:
                            if intent.get("package_action") == "install":
                                if ACTIONS[e["action"]]:
                                    extras.append(Step(ACTIONS[e["action"]]))
                            else:
                                notes.append(self._m("action_skipped", name=cid))
                        elif "unsupported" in e:
                            unsupported = True
                    fidelity = _worst(FIDELITY_ORDER, fidelity,
                                      (concept.get("fidelity") or {}).get(fam, "exact"))
                    contributed = (len(names), len(extras)) != before
                    if concept.get("note", {}).get(fam) and (contributed or unsupported):
                        notes.append(self._text(concept["note"][fam]))
                argvals[name] = names
                if spec.get("min", 0) > 0 and not names:
                    skip_main = True
            elif t == "service":
                svc = self.rules.services[v]
                unit = svc["units"].get(fam)
                if svc.get("note", {}).get(fam):
                    notes.append(self._text(svc["note"][fam]))
                if unit is None:
                    return _Seg("refused", notes=[self._m("no_family_target", name=v, family=fam)],
                                reason="unknown_name")
                if unit == "!":
                    unsupported = True
                else:
                    argvals[name] = unit
            else:
                argvals[name] = list(v) if isinstance(v, tuple) else v
        if unsupported:
            return _Seg("unsupported", fidelity="unsupported", risk=intent["risk"], notes=notes)
        if "note" in hform:
            notes.append(self._text(hform["note"]))

        steps = []
        if not skip_main:
            argv = _expand(hform["render"], argvals, pr.flags)
            root = hform.get("root", "optional")
            use_sudo = False
            if root == "required":
                use_sudo = (not self.host.is_root) and bool(self.host.has.get("sudo"))
                if not self.host.is_root and not self.host.has.get("sudo"):
                    notes.append(self._m("no_sudo"))
            elif root == "optional":
                use_sudo = had_sudo and bool(self.host.has.get("sudo")) and not self.host.is_root
            elif had_sudo:
                notes.append(self._m("sudo_removed"))
            steps.append(Step((["sudo"] if use_sudo else []) + argv))
        steps.extend(extras)
        return _Seg("translated", steps, ["&&"] * max(0, len(steps) - 1), fidelity,
                    intent["risk"], notes)


def _expand(tokens, argvals, flags):
    out = []
    for t in tokens:
        if isinstance(t, str):
            out.append(t)
        elif "ref" in t:
            out.append(argvals[t["ref"]])
        elif "splat" in t:
            out.extend(argvals[t["splat"]])
        else:
            branch = t.get("then", []) if t["if"] in flags else t.get("else", [])
            out.extend(_expand(branch, argvals, flags))
    return out


def _has_evidence(form, values):
    ev = form["match"].get("evidence")
    if not ev:
        return False
    for item in ev:
        if not any(values[k] == item["after_flag"] and values[k + 1] == item["token"]
                   for k in range(len(values) - 1)):
            return False
    return True


def _signature(intent, form, pr, res):
    return (intent["id"], frozenset(pr.flags), tuple(sorted(pr.flag_values.items())),
            tuple(sorted(res.items())))


def _confirm(risk, steps):
    if not steps or risk is None:
        return "none"
    return {"destructive": "typed", "modify": "yn", "read": "enter"}[risk]


def _dedupe(items):
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def translate(text, host, rules, lang="en"):
    return Translator(rules, host, lang).translate(text)
