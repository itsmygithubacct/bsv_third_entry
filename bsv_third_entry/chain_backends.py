"""chain_c-backed receipt broadcast backend (the Third Entry *publish* step).

Drop-in for ``trinote.receipts.WalletThirdEntryBackend``: same ``broadcast(artifact, ts=...)``
contract, but the publish goes through the chain_c CLIs instead of the Python BSV wallet / the
vendored TypeScript ``chain/``. The engine's ``run_bonsai_cli`` constructs
``WalletThirdEntryBackend(...)`` for ``--onchain``; the bridge (:mod:`bsv_third_entry.engine_run`)
rebinds that name to this class, so ``--onchain`` flows through chain_c with no edit to the engine.

Two modes (default **resumable**):

  * ``resumable`` — one metered ``agentd action`` under a **persisted, pre-deployed identity**
    (``$BONSAI_NOTARY_HOME/agent/identity.state.json``). Deploy the identity once
    (``bonsai-agent deploy --confirm``); thereafter every receipt is a cheap state transition of the
    same reputation-bearing identity — no per-receipt deploy/revoke. This is what binds a stream of
    receipts to one on-chain agent.
  * ``oneshot`` — the self-contained ``bonsai_third_entry`` lifecycle (deploy → executeAction →
    revoke) with ephemeral keys; no persisted identity. Select with ``mode="oneshot"`` or
    ``BONSAI_THIRD_ENTRY_MODE=oneshot``.

Two-key interlock (both modes): DRY-RUN by default; a real spend needs ``confirm=True`` AND the C
binary's own ``CONFIRM_MAINNET_BROADCAST=yes`` (this backend sets the latter only when confirmed).

Receipt→chain binding:
  * ``ACTION_HASH``     = the Bonsai ``receiptHash``
  * ``PROVENANCE_HASH`` = the Bonsai ``modelHash``. The model hash is mandatory and nonzero; an
                          explicit ``provenanceHash`` must equal it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from . import _parse, paths
from .agentd import (ActionRecordRecoveryError, AgentdError, ChainCAgentd,
                     recover_action_record)

_HEX32 = re.compile(r"\A[0-9a-fA-F]{64}\Z")


class ChainCBroadcastError(RuntimeError):
    """A chain_c broadcast attempt failed (binary missing, non-zero exit, or unparseable output)."""


class ChainCBroadcastUncertain(ChainCBroadcastError):
    """A real broadcast was attempted but its txid could not be recovered from the C output.

    The transaction MAY ALREADY BE ON-CHAIN. This is deliberately a distinct error from a plain
    failure so a caller does NOT treat it as "nothing happened" and blindly re-run — re-running
    could DOUBLE-SPEND. The operator must verify on-chain before any retry.
    """


def _content_commit(artifact: dict) -> str:
    """Deterministic content commitment over the chain artifact.

    Prefers the engine's canonical ``commit`` (so a dry-run txid matches the rest of the system) and
    falls back to a stdlib sorted-JSON SHA-256 when the engine is not importable.
    """
    try:  # pragma: no cover - exercised when the engine is on PYTHONPATH
        from trinote.receipts.canonical import commit  # type: ignore

        return commit(artifact)
    except Exception:
        blob = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


def _require_hash32(name: str, value: str) -> str:
    """Fail closed unless *value* is a 32-byte (64-hex-char) hash; normalize to lowercase."""
    if not isinstance(value, str) or not _HEX32.match(value):
        raise ChainCBroadcastError(f"{name} must be a 32-byte hex string (64 hex chars), got {value!r}")
    return value.lower()


class ChainCThirdEntryBackend:
    """Publish a receipt's Third Entry via chain_c — a resumable ``agentd action`` by default.

    Accepts (and ignores) the keyword args the engine passes to ``WalletThirdEntryBackend``
    (``source_index``, ``sat_per_kb``, ``change_to_source``, ``allow_unconfirmed``) so it is a true
    drop-in; ``confirm`` and ``mode`` change behaviour.
    """

    def __init__(self, *, confirm: bool = False, timeout: int = 600, mode: str | None = None,
                 state_file: str | Path | None = None, amount: int = 1000, network: str = "main",
                 elder_key_file: str | None = None, agent_key_file: str | None = None,
                 counterparty_key_file: str | None = None, fund_action_key_file: str | None = None,
                 change_address: str | None = None, ricardian_hash: str | None = None,
                 **_ignored_wallet_kwargs):
        self.confirm = confirm
        self.timeout = timeout
        self.mode = (mode or os.environ.get("BONSAI_THIRD_ENTRY_MODE") or "resumable").lower()
        # amount: the metered action cost for resumable mode (agentd reads $AMOUNT). The one-shot
        # bonsai_third_entry binary uses its own fixed action cost and ignores this.
        self.amount = amount
        self.ricardian_hash = ricardian_hash
        self.state_file = (Path(state_file).expanduser() if state_file
                           else paths.notary_home() / "agent" / "identity.state.json")
        # one-shot transport
        self.binary = paths.chain_c_bin("bonsai_third_entry")
        # resumable transport
        self._agent = ChainCAgentd(
            state_file=self.state_file, network=network, confirm=confirm, timeout=timeout,
            elder_key_file=elder_key_file, agent_key_file=agent_key_file,
            counterparty_key_file=counterparty_key_file, fund_action_key_file=fund_action_key_file,
            change_address=change_address)

    @property
    def label(self) -> str:
        return "chain_c:bonsai_third_entry" if self.mode == "oneshot" else "chain_c:agentd-action"

    def _hashes(self, artifact: dict) -> tuple[str, str]:
        receipt_hash = artifact.get("receiptHash")
        if not receipt_hash:
            raise ChainCBroadcastError("chain artifact has no receiptHash to anchor")
        action_hash = _require_hash32("receiptHash", receipt_hash)
        model_hash = artifact.get("modelHash")
        if not model_hash:
            raise ChainCBroadcastError("chain artifact has no modelHash to bind as provenance")
        model_hash = _require_hash32("modelHash", model_hash)
        if model_hash == "0" * 64:
            raise ChainCBroadcastError("modelHash must bind a real model artifact; all-zero is not allowed")
        if artifact.get("provenanceHash") is not None:
            provenance = _require_hash32("provenanceHash", artifact["provenanceHash"])
            if provenance != model_hash:
                raise ChainCBroadcastError("provenanceHash does not match the receipt modelHash")
        return action_hash, model_hash

    # -- the WalletThirdEntryBackend contract -----------------------------------------------
    def broadcast(self, artifact: dict, *, ts: str | None = None) -> dict:
        if self.mode == "oneshot":
            return self._broadcast_oneshot(artifact, ts=ts)
        return self._broadcast_resumable(artifact, ts=ts)

    # -- resumable: one metered agentd action under the persisted identity ------------------
    def _broadcast_resumable(self, artifact: dict, *, ts: str | None) -> dict:
        action_hash, provenance_hash = self._hashes(artifact)
        # Label the anchor with the ACTUAL configured network, not a hardcoded 'livenet'
        # (review finding #22): a testnet run was mislabeled as mainnet, so a downstream
        # consumer trusting this field would treat a worthless testnet anchor as real.
        # Mirror chain_c's network selection EXACTLY (agentd_lib.c routes startswith("test")->testnet,
        # everything else -> mainnet), so a non-canonical NETWORK that chain_c broadcasts on real
        # mainnet is never under-labeled here as testnet (review-2 finding #6).
        net_label = "testnet" if str(self._agent.network or "main").startswith("test") else "livenet"
        rec = {"backend": self.label, "network": net_label, "ts": ts,
               "actionHash": action_hash, "provenanceHash": provenance_hash,
               "stateFile": str(self.state_file)}

        if not self.state_file.exists():
            reason = (f"no deployed agent identity at {self.state_file}; run "
                      f"`bonsai-agent deploy --confirm` (or `bsv-agent deploy`) once before anchoring "
                      f"receipts in resumable mode (or use mode='oneshot')")
            if self.confirm:
                raise ChainCBroadcastError(reason)
            rec.update(txid="dryrun:" + _content_commit(artifact)[:32], status="dry-run",
                       broadcast=False, dryRun=True, identity="absent", reason=reason)
            return rec

        # Keep the pre-action state in memory. Legacy chain_c builds do not print a structured
        # action record, so a successful live run is recovered by proving the signed transaction
        # advances this exact tip to the post-action state.
        try:
            pre_state = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            pre_state = None
        try:
            proc, record = self._agent.action(action_hash=action_hash,
                                              provenance_hash=provenance_hash, amount=self.amount)
        except AgentdError as exc:
            raise ChainCBroadcastError(str(exc)) from exc
        out = proc.stdout
        rec["plan"] = out.strip()
        rec["identity"] = "deployed"

        if not self.confirm or _parse.is_dry_run(out):
            rec.update(txid="dryrun:" + _content_commit(artifact)[:32], status="dry-run",
                       broadcast=False, dryRun=True)
            return rec

        # A real broadcast was attempted here (confirm=True and not a dry-run). Prefer the
        # structured actionTxid, then fall back to scraping stdout markers.
        txid = ((record or {}).get("actionTxid")
                or _parse.txid_after(out, "BROADCAST OK")
                or _parse.txid_after(out, "EXECUTE broadcast"))
        if not txid:
            # FINDING #8: the broadcast may have SUCCEEDED but we lost the txid. Fail with a
            # distinct error so the caller does not treat it as a clean failure and re-run (which
            # could double-spend). The operator must verify on-chain BEFORE any retry.
            raise ChainCBroadcastUncertain(
                "agentd action was confirmed and broadcast, but no txid could be recovered from "
                "its output: the transaction MAY ALREADY BE ON-CHAIN. Verify on-chain BEFORE "
                "retrying — do NOT auto-retry, as re-running may double-spend.\n"
                f"agentd stdout:\n{out.strip()}")
        rec.update(txid=txid, status="broadcast", broadcast=True)
        if record is None and isinstance(pre_state, dict):
            try:
                record = recover_action_record(
                    out, state_file=self.state_file, pre_state=pre_state, action_txid=txid,
                    expected_action_hash=action_hash,
                    expected_provenance_hash=provenance_hash, expected_amount=self.amount,
                )
                rec["recordSource"] = "verified-flat-emission"
            except ActionRecordRecoveryError as exc:
                # The transaction is already broadcast. Preserve that fact and make the missing
                # bundle record explicit; never misreport this as a safe-to-retry failure.
                rec["recordRecoveryError"] = str(exc)
        if record:
            rec["record"] = record
        return rec

    # -- one-shot: self-contained AgentTea deploy->executeAction->revoke --------------------
    def _oneshot_env(self, artifact: dict) -> dict:
        action_hash, provenance_hash = self._hashes(artifact)
        env = dict(os.environ)
        env["BONSAI_NOTARY_HOME"] = str(paths.notary_home())
        env["BONSAI_AGENT_TEA_ARTIFACT"] = str(paths.agent_tea_artifact())
        env["AGENT_TEA_ARTIFACT"] = str(paths.agent_tea_artifact())
        env["ACTION_HASH"] = action_hash
        env["PROVENANCE_HASH"] = provenance_hash
        if self.ricardian_hash:
            env["RICARDIAN_HASH"] = _require_hash32("ricardianHash", self.ricardian_hash)
        if self.confirm:
            env["CONFIRM_MAINNET_BROADCAST"] = "yes"
        else:
            env.pop("CONFIRM_MAINNET_BROADCAST", None)
        return env

    def command(self, artifact: dict) -> list[str]:
        """The argv the one-shot mode would exec (exposed for tests / dry inspection)."""
        return [str(self.binary)]

    def _broadcast_oneshot(self, artifact: dict, *, ts: str | None) -> dict:
        if not self.binary.exists():
            raise ChainCBroadcastError(
                f"chain_c binary not found: {self.binary}\n"
                f"  build it: bash {paths.chain_c_dir()}/build_chain_c.sh   (-> $BONSAI_NOTARY_HOME/chain_c/build)")
        env = self._oneshot_env(artifact)
        try:
            proc = subprocess.run(self.command(artifact), cwd=str(paths.chain_c_dir()),
                                  env=env, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ChainCBroadcastError(f"failed to launch {self.binary}: {exc}") from exc
        out, err = proc.stdout, proc.stderr
        if proc.returncode != 0:
            raise ChainCBroadcastError(
                f"bonsai_third_entry failed (exit {proc.returncode}):\n{(err or out).strip()}")

        rec = {"backend": self.label, "network": _parse.field(out, "Network") or "livenet", "ts": ts,
               "actionHash": env["ACTION_HASH"], "provenanceHash": env["PROVENANCE_HASH"],
               "ricardianHash": _parse.field(out, "ricardianHash"), "elder": _parse.field(out, "Elder"),
               "plan": out.strip()}
        if not self.confirm or _parse.is_dry_run(out):
            rec.update(txid="dryrun:" + _content_commit(artifact)[:32], status="dry-run",
                       broadcast=False, dryRun=True)
            return rec
        # A real broadcast was attempted here (confirm=True and not a dry-run).
        txid = (_parse.txid_after(out, "EXECUTE broadcast")
                or _parse.txid_after(out, "BROADCAST OK")
                or _parse.txid_after(out, "DEPLOY broadcast"))
        if not txid:
            # FINDING #8: same uncertainty as the resumable path — the broadcast may have
            # SUCCEEDED but we lost the txid. Distinct error so the caller does not re-run and
            # double-spend; the operator must verify on-chain BEFORE any retry.
            raise ChainCBroadcastUncertain(
                "bonsai_third_entry was confirmed and broadcast, but no txid could be recovered "
                "from its output: the transaction MAY ALREADY BE ON-CHAIN. Verify on-chain BEFORE "
                "retrying — do NOT auto-retry, as re-running may double-spend.\n"
                f"bonsai_third_entry stdout:\n{out.strip()}")
        rec.update(txid=txid, status="broadcast", broadcast=True)
        return rec
