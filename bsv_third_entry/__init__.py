"""bsv_third_entry — agent / on-chain orchestration for deterministic-inference receipts.

This package is the **on-chain orchestration layer**: it turns a Bonsai inference receipt into a
Bitcoin SV *Third Entry* by driving the chain_c C CLIs (``bonsai_third_entry`` / ``agentd``). It is
deliberately independent of any particular inference engine and of the larger ``bonsai-notary``
composition — it only needs a built ``chain_c`` checkout and the receipt's commitment hashes.

What's here:
  * :class:`bsv_third_entry.chain_backends.ChainCThirdEntryBackend` — a drop-in for an ``trinote``
    ``WalletThirdEntryBackend``: ``broadcast(artifact, ts=...)`` execs ``chain_c/build/bonsai_third_entry``
    (the AgentTea deploy→executeAction→revoke Third Entry). DRY-RUN unless ``confirm=True``.
  * :class:`bsv_third_entry.agentd.ChainCAgentd` — the resumable AgentTea identity lifecycle
    (``deploy`` / ``action`` / ``revoke`` / ``status``) over ``chain_c/build/agentd``.
  * :mod:`bsv_third_entry.agent_cli` — ``bsv-agent`` CLI for that lifecycle.
  * :mod:`bsv_third_entry.engine_run` — optional bridge that runs an ``trinote`` engine's
    ``run_bonsai_cli`` with this third-entry backend wired in as the ``--onchain`` publisher.

The two-key interlock is preserved everywhere: DRY-RUN by default, and a real broadcast needs both
an explicit ``confirm`` AND the binary's own ``CONFIRM_MAINNET_BROADCAST=yes`` gate.
"""
from __future__ import annotations

__all__ = ["paths", "chain_backends", "agentd", "agent_cli", "engine_run"]
__version__ = "0.1.0"
