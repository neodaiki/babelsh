"""Result types of translate()."""
import shlex


class Step(object):
    """One command. `raw` is set when the text must be reproduced verbatim (untouched
    segments may contain $VAR, redirects ...); otherwise the command is argv."""
    __slots__ = ("argv", "raw", "redir")

    def __init__(self, argv, raw=None, redir=""):
        self.argv = list(argv)
        self.raw = raw
        self.redir = redir    # simple redirects carried over from the input, e.g. "> log 2>&1"

    def __eq__(self, other):
        return (isinstance(other, Step) and self.argv == other.argv and self.raw == other.raw
                and self.redir == other.redir)

    def __repr__(self):
        extra = "" if self.raw is None else ", raw=%r" % self.raw
        extra += ", redir=%r" % self.redir if self.redir else ""
        return "Step(%r%s)" % (self.argv, extra)

    def shell(self):
        if self.raw is not None:
            return self.raw
        text = " ".join(shlex.quote(a) for a in self.argv)
        return text + (" " + self.redir if self.redir else "")


class Plan(object):
    """status: translated | noop | passthrough | refused | unsupported

    translated:  run `steps` joined by `joins`.
    noop:        translation succeeded but there is nothing to run on this host.
    passthrough: run `original` exactly as typed.
    refused:     could not translate safely; run nothing. `reason` is a machine code.
    unsupported: no equivalent exists on this host; run nothing.
    """

    def __init__(self, status, original="", steps=None, joins=None, fidelity=None,
                 risk=None, confirm="none", notes=None, reason=None):
        self.status = status
        self.original = original
        self.steps = steps or []
        self.joins = joins or []
        self.fidelity = fidelity
        self.risk = risk
        self.confirm = confirm
        self.notes = notes or []
        self.reason = reason

    def __repr__(self):
        return "Plan(%s)" % ", ".join("%s=%r" % kv for kv in sorted(self.__dict__.items()))


def to_shell(plan):
    """Command line to show/run for a plan. Only meaningful for translated/passthrough."""
    if plan.status == "passthrough":
        return plan.original
    out = []
    for idx, step in enumerate(plan.steps):
        if idx:
            op = plan.joins[idx - 1]
            out.append("; " if op == ";" else " %s " % op)
        out.append(step.shell())
    return "".join(out)
