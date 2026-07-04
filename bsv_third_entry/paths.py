"""Path resolution for the on-chain orchestration.

Engine-agnostic: this package only needs to find a built ``chain_c`` checkout, the shared state
home (where the chain key files live), and the committed AgentTea artifact. Every path has an env
override so the same code runs standalone (with the repo's own ``chain_c`` symlink) or embedded in
``bonsai-notary`` (which exports ``BONSAI_CHAIN_C_DIR`` / ``BONSAI_NOTARY_HOME``).
"""
from __future__ import annotations

import os
from pathlib import Path

# <repo>/bsv_third_entry/paths.py -> <repo> is the project root.
REPO_ROOT = Path(__file__).resolve().parents[1]


def _first_existing(*candidates: Path) -> Path:
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


def chain_c_dir() -> Path:
    """The chain_c checkout (its CLIs live under ``<dir>/build``).

    ``$BONSAI_CHAIN_C_DIR`` wins; else the repo's own ``chain_c`` symlink; else ``~/chain_c``.
    """
    env = os.environ.get("BONSAI_CHAIN_C_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return _first_existing(REPO_ROOT / "chain_c", Path.home() / "chain_c")


def chain_c_build_dir() -> Path:
    """Where the built chain_c CLIs live — OUTSIDE the source tree.

    ``$BONSAI_CHAIN_C_BUILD`` wins; else ``$BONSAI_NOTARY_HOME/chain_c/build`` (default
    ``~/.local/trinote/chain_c/build``). Keeping binaries under the notary home mirrors the native
    kernel (``$BONSAI_NOTARY_HOME/bin``): build artifacts are not source and never live in the checkout.
    """
    env = os.environ.get("BONSAI_CHAIN_C_BUILD")
    if env:
        return Path(env).expanduser().resolve()
    return notary_home() / "chain_c" / "build"


def chain_c_bin(name: str) -> Path:
    """A built on-chain CLI under the chain_c build dir (``bonsai_third_entry``, ``agentd``, …)."""
    return chain_c_build_dir() / name


def notary_home() -> Path:
    """The shared state/secrets home (``$BONSAI_NOTARY_HOME``, default ``~/.local/trinote``).

    Holds the chain_c ``chain/*.json`` key files (and, in the larger composition, the engine's
    receipt ledger + signing keys). Never inside the repo; never copied/shipped.
    """
    env = os.environ.get("BONSAI_NOTARY_HOME")
    return Path(env).expanduser() if env else Path.home() / ".local" / "trinote"


def agent_tea_artifact() -> Path:
    """The committed AgentTea contract artifact chain_c reconstructs the locking script from.

    ``$BONSAI_AGENT_TEA_ARTIFACT`` wins; else the one vendored in the chain_c checkout. Absolute,
    because the CLIs are run with ``cwd=chain_c`` but read this explicitly by env.
    """
    env = os.environ.get("BONSAI_AGENT_TEA_ARTIFACT")
    if env:
        return Path(env).expanduser().resolve()
    return chain_c_dir() / "artifacts" / "src" / "contracts-next" / "agentTea.json"
