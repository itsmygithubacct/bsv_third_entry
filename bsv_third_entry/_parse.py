"""Tiny parsers for the human-readable stdout the chain_c CLIs print.

The C binaries are faithful ports of the TS ``console.log`` scripts: they print labelled
``label   : value`` lines and, on a live broadcast, ``STEP broadcast    : <txid>`` /
``BROADCAST OK: <txid>``. These helpers pull values back out so the Python backends can
return a structured record. Kept separate so they unit-test with sample text — no binary,
no chain.
"""
from __future__ import annotations

import re

_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")


def field(out: str, label: str) -> str | None:
    """Return the value of the first ``<label> ... : <value>`` line, stripped.

    Matches the chain_c plan format, e.g. ``ricardianHash  : c0d6...`` → ``c0d6...``.
    The label is matched at the start of the (left-stripped) line; everything after the
    first ``:`` is the value.
    """
    for line in out.splitlines():
        s = line.strip()
        if s.startswith(label):
            rest = s[len(label):]
            # Guard the prefix match: a real label is followed by whitespace or the ``:``
            # separator, so e.g. label ``Network`` must not bind to a longer ``NetworkId`` line.
            if rest and not (rest[0].isspace() or rest[0] == ":"):
                continue
            rest = rest.lstrip()
            if rest.startswith(":"):
                return rest[1:].strip()
    return None


def txid_after(out: str, marker: str) -> str | None:
    """Return the 64-hex txid on the first line containing *marker* (e.g. ``EXECUTE broadcast``).

    chain_c prints ``[2/3] EXECUTE broadcast   : <txid>`` and ``BROADCAST OK: <txid>``; we
    just find the marker line and extract the first 64-hex token on it.
    """
    for line in out.splitlines():
        if marker in line:
            m = _HEX64.search(line)
            if m:
                return m.group(0).lower()
    return None


def is_dry_run(out: str) -> bool:
    """True when the CLI stopped at its dry-run gate (no broadcast happened).

    Source of truth is the *broadcast txid*, not free text. Every live chain_c path prints a
    structured ``BROADCAST OK: <txid>`` / ``EXECUTE broadcast: <txid>`` line — the same txid the
    backend reads back as ``record.actionTxid`` — and it prints that ONLY after the dry-run gate has
    been passed (the gate prints ``DRY RUN — …`` and returns first). So if such a txid is recoverable
    the run was LIVE: report not-dry-run even if the surrounding output happens to contain the words
    "dry run". Otherwise a live broadcast could be misreported as a dry-run and an automated caller
    might re-broadcast → double-spend.

    With no broadcast txid present, anchor the marker to the exact token the C CLIs emit at the START
    of a line (``DRY RUN — …``) instead of matching the substring anywhere in the output; the
    ``DRY-RUN`` spelling is accepted too for parity with the TS scripts' wording.
    """
    if txid_after(out, "BROADCAST OK") or txid_after(out, "EXECUTE broadcast"):
        return False
    for line in out.splitlines():
        head = line.strip().upper()
        if head.startswith("DRY RUN") or head.startswith("DRY-RUN"):
            return True
    return False
