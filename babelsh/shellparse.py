"""Split a command line into segments and tokens, without executing or expanding anything.

Only what translation needs: segments separated by && || ; | and newline, quote-aware
tokens, and flags saying whether a segment contains syntax we do not model (redirects,
background, subshell, comments, env-assignment prefixes) or tokens computed by the shell
($VAR, $(...), `...`, globs).
"""
import re
from collections import namedtuple

Token = namedtuple("Token", ["value", "dynamic"])

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class ShellParseError(Exception):
    pass


class Segment(object):
    __slots__ = ("tokens", "raw", "op", "syntax", "redirs")

    def __init__(self, tokens, raw, op, syntax, redirs=()):
        self.tokens = tokens
        self.raw = raw
        self.op = op          # operator that follows this segment, or None
        self.syntax = syntax  # contains syntax this parser does not model
        self.redirs = list(redirs)  # simple redirects with literal targets, e.g. ["2>&1", ">log"]

    def __repr__(self):
        return "Segment(%r, op=%r, syntax=%r)" % ([t.value for t in self.tokens], self.op, self.syntax)


class _Lexer(object):
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.segments = []
        self.toks = []
        self.word = []
        self.in_word = False
        self.dyn = False
        self.syntax = False
        self.redirs = []
        self.seg_start = 0

    # -- word building -------------------------------------------------
    def _add(self, text, dynamic=False):
        self.word.append(text)
        self.in_word = True
        if dynamic:
            self.dyn = True

    def _flush_word(self):
        if self.in_word:
            self.toks.append(Token("".join(self.word), self.dyn))
        self.word = []
        self.in_word = False
        self.dyn = False

    # -- segments ------------------------------------------------------
    def _end_segment(self, op, text_end):
        self._flush_word()
        raw = self.s[self.seg_start:text_end].strip()
        if not self.toks:
            if op is not None:
                raise ShellParseError("empty command before %r" % op)
        else:
            syntax = self.syntax or bool(_ASSIGN.match(self.toks[0].value))
            self.segments.append(Segment(self.toks, raw, op, syntax, self.redirs))
        self.toks = []
        self.syntax = False
        self.redirs = []

    def _operator(self, op, length):
        self._end_segment(op, self.i)
        self.i += length
        self.seg_start = self.i

    # -- expansions ----------------------------------------------------
    def _match_paren(self, open_idx):
        depth = 0
        for k in range(open_idx, len(self.s)):
            c = self.s[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return k + 1
        raise ShellParseError("unterminated $(")

    def _dollar(self):
        s, i = self.s, self.i
        n = len(s)
        nxt = s[i + 1] if i + 1 < n else ""
        if nxt == "(":
            j = self._match_paren(i + 1)
            self._add(s[i:j], True)
            self.i = j
        elif nxt == "{":
            j = s.find("}", i + 2)
            if j < 0:
                raise ShellParseError("unterminated ${")
            self._add(s[i:j + 1], True)
            self.i = j + 1
        elif nxt and (nxt.isalpha() or nxt == "_"):
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            self._add(s[i:j], True)
            self.i = j
        elif nxt and nxt in "@*#?!$-0123456789":
            self._add(s[i:i + 2], True)
            self.i = i + 2
        else:
            self._add("$")
            self.i = i + 1

    def _backtick(self):
        j = self.s.find("`", self.i + 1)
        if j < 0:
            raise ShellParseError("unterminated backtick")
        self._add(self.s[self.i:j + 1], True)
        self.i = j + 1

    def _double_quoted(self):
        s, n = self.s, len(self.s)
        self.in_word = True
        self.i += 1
        while self.i < n:
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self.i + 1 < n:
                nxt = s[self.i + 1]
                if nxt in '"\\$`':
                    self._add(nxt)
                elif nxt == "\n":
                    pass
                else:
                    self._add("\\" + nxt)
                self.i += 2
            elif c == "$":
                self._dollar()
            elif c == "`":
                self._backtick()
            else:
                self._add(c)
                self.i += 1
        raise ShellParseError("unterminated double quote")

    def _redirect(self):
        """Consume `[fd]OP target`. Simple redirects with a literal target are recorded in
        self.redirs; anything else (heredoc, quoted/computed target) marks the segment as syntax."""
        s, n = self.s, len(self.s)
        fd = ""
        if self.in_word and "".join(self.word).isdigit() and not self.dyn:
            fd = "".join(self.word)
            self.word, self.in_word = [], False
        else:
            self._flush_word()
        op = None
        for cand in ("&>>", "&>", ">>", ">&", "<&", "<<", ">", "<"):
            if s.startswith(cand, self.i):
                op = cand
                break
        self.i += len(op)
        if op == "<<":
            self.syntax = True
            return
        while self.i < n and s[self.i] in " \t":
            self.i += 1
        start = self.i
        if op.endswith("&") and op != "&>":
            while self.i < n and (s[self.i].isdigit() or s[self.i] == "-"):
                self.i += 1
        else:
            while self.i < n and s[self.i] not in " \t\n;&|<>()'\"`$\\*?[#":
                self.i += 1
        target = s[start:self.i]
        ends_cleanly = self.i >= n or s[self.i] in " \t\n;&|<>"
        if not target or not ends_cleanly:
            self.syntax = True
            return
        self.redirs.append("%s%s%s" % (fd, op, target))

    # -- main loop -----------------------------------------------------
    def run(self):
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in " \t":
                self._flush_word()
                self.i += 1
            elif c == "\n":
                self._operator(";", 1)
            elif c == "'":
                j = s.find("'", self.i + 1)
                if j < 0:
                    raise ShellParseError("unterminated single quote")
                self._add(s[self.i + 1:j])
                self.i = j + 1
            elif c == '"':
                self._double_quoted()
            elif c == "\\":
                if self.i + 1 >= n:
                    raise ShellParseError("trailing backslash")
                if s[self.i + 1] != "\n":
                    self._add(s[self.i + 1])
                self.i += 2
            elif c == "$":
                self._dollar()
            elif c == "`":
                self._backtick()
            elif c in "*?[":
                self._add(c, True)
                self.i += 1
            elif c == "#" and not self.in_word:
                self.syntax = True
                self._add(s[self.i:])
                self.i = n
            elif c == "&":
                if s.startswith("&&", self.i):
                    self._operator("&&", 2)
                elif s.startswith("&>", self.i):
                    self._redirect()
                else:
                    self.syntax = True  # background job
                    self._add(c)
                    self.i += 1
            elif c == "|":
                if s.startswith("||", self.i):
                    self._operator("||", 2)
                else:
                    self._operator("|", 1)
            elif c == ";":
                self._operator(";", 1)
            elif c in "<>":
                self._redirect()
            elif c in "()":
                self.syntax = True
                self._add(c)
                self.i += 1
            else:
                self._add(c)
                self.i += 1
        self._end_segment(None, n)
        return self.segments


def split(text):
    """Return a list of Segment. Raises ShellParseError on input we cannot tokenize."""
    return _Lexer(text).run()
