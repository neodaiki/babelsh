"""ush: translate package/service/coreutils commands between Debian, Fedora and macOS.

Client code is stdlib-only and must stay compatible with Python 3.8.
"""
from .plan import Plan, Step, to_shell
from .rules import Rules, RulesError, load_rules
from .translate import translate

__all__ = ["Plan", "Step", "to_shell", "Rules", "RulesError", "load_rules", "translate"]
from ._version import __version__  # noqa: E402
