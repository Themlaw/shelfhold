"""Sablier (on-demand start of services): the only caller of Sablier (`sablier-confined`).

`SablierHook` is the adapter of the `WakeHook` port (EF-INT-022, ADR-0025). Every call goes
through the Fetcher it is given, so the host allowlist, the address checks and the rule that no
redirection reaches an undeclared private address apply to it as to any other outbound call.
"""

from shelfhold.integrations.sablier.hook import SablierHook, SablierTarget

__all__ = ["SablierHook", "SablierTarget"]
