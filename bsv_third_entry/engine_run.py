"""Bridge: run an ``trinote`` inference engine with chain_c as the ``--onchain`` Third Entry backend.

The engine's ``trinote.cli.run_bonsai_cli`` hard-constructs ``WalletThirdEntryBackend(...)`` for
``--onchain``. Rather than fork the engine (it is pulled in read-only and will come from its own
GitHub repo), we import the module and *rebind* that one name to
:class:`bsv_third_entry.chain_backends.ChainCThirdEntryBackend` before calling ``main()`` — so
``--onchain`` publishes through ``chain_c/build/bonsai_third_entry`` with zero changes to the engine
tree.

The engine package is located via ``$BONSAI_ENGINE_DIR`` (its ``trinote`` lives under
``<dir>/bonsai/src``) or it is assumed to already be on ``PYTHONPATH``.

Run:  python -m bsv_third_entry.engine_run <run_bonsai_cli args...>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_engine_on_path() -> None:
    """Put the engine's ``trinote`` package on ``sys.path`` from ``$BONSAI_ENGINE_DIR`` if set."""
    engine_dir = os.environ.get("BONSAI_ENGINE_DIR")
    if engine_dir:
        src = Path(engine_dir).expanduser() / "bonsai" / "src"
        if src.is_dir() and str(src) not in sys.path:
            sys.path.insert(0, str(src))


def main() -> int:
    _ensure_engine_on_path()

    try:
        import trinote.cli.run_bonsai_cli as rbc
    except ImportError as exc:  # pragma: no cover - environment/wiring error
        sys.stderr.write(
            f"[bsv-third-entry] cannot import the inference engine (trinote): {exc}\n"
            f"  set BONSAI_ENGINE_DIR to the engine checkout (trinote under <dir>/bonsai/src),\n"
            f"  or put that src dir on PYTHONPATH.\n")
        return 3

    from .chain_backends import ChainCThirdEntryBackend

    if not hasattr(rbc, "WalletThirdEntryBackend"):
        sys.stderr.write(
            "[bsv-third-entry] engine API changed: run_bonsai_cli no longer exposes "
            "WalletThirdEntryBackend; update engine_run.py to match the new on-chain seam.\n")
        return 3

    # Capture the ORIGINAL (real, broadcasting) wallet backend before we rebind anything.
    # run_bonsai_cli does ``from ..receipts import WalletThirdEntryBackend``, so rbc's attribute
    # currently *is* that original class object.
    original = rbc.WalletThirdEntryBackend

    # Happy path: rebind the name the engine actually constructs.
    rbc.WalletThirdEntryBackend = ChainCThirdEntryBackend  # type: ignore[attr-defined]

    # FINDING #4 — fail closed if interception is ever bypassed. The class is *defined* in
    # ``trinote.receipts.broadcast`` and re-exported via ``trinote.receipts``; a factory or
    # ``import trinote.receipts.broadcast as m; m.WalletThirdEntryBackend(...)`` alias could
    # construct the original wallet backend without going through rbc's rebound name. Rebind the
    # name at every already-imported module that still points at the original so those aliases are
    # intercepted too. (Use sys.modules — these modules are already loaded because importing
    # run_bonsai_cli imported them — so we never trigger a fresh import or mask an ImportError.)
    for modname in ("trinote.receipts.broadcast", "trinote.receipts"):
        mod = sys.modules.get(modname)
        if mod is not None and getattr(mod, "WalletThirdEntryBackend", None) is original:
            mod.WalletThirdEntryBackend = ChainCThirdEntryBackend  # type: ignore[attr-defined]

    # The key safety net: neutralize the ORIGINAL class itself. If any construction path slips
    # past the rebinds above, this makes constructing the real wallet backend raise loudly during
    # this run instead of silently broadcasting (spending) through the BSV wallet.
    def _refuse_real_wallet(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError(
            "chain_c rebind did not intercept the on-chain backend; refusing to fall back to the "
            "real wallet backend (WalletThirdEntryBackend). This run would have broadcast through "
            "the BSV wallet — aborting before any spend.")

    # Idempotency guard: on a second main() call in the same process, `original` is already
    # ChainCThirdEntryBackend (rebound by the first call), so neutralizing *its* __init__ would
    # brick the chain_c backend itself. Only neutralize when `original` is the real wallet backend.
    if original is not ChainCThirdEntryBackend:
        try:
            original.__init__ = _refuse_real_wallet  # type: ignore[method-assign]
        except (TypeError, AttributeError):  # pragma: no cover - exotic/immutable class object
            pass

    rc = rbc.main()
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
