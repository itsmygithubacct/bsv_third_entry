"""Drive the resumable AgentTea lifecycle via the chain_c ``agentd`` CLI.

Ported from the engine's ``trinote.agent.orchestrator`` (which the standalone engine no longer
ships): the only change is the transport — instead of ``npx ts-node scripts/agentd.ts <cmd>`` in
the vendored TypeScript ``chain/`` dir, it execs ``chain_c/build/agentd <cmd>`` in the chain_c
checkout. chain_c's ``agentd`` is a byte-faithful C port of ``agentd.ts`` and reads the *same*
environment contract (``ELDER_KEY_FILE``, ``AGENT_KEY_FILE``, …, ``RICARDIAN_HASH``,
``ACTION_HASH``, ``PROVENANCE_HASH``, ``CONFIRM_MAINNET_BROADCAST``), so the orchestration logic,
the action-record parser, and the bundle-input transform carry over unchanged.

DRY-RUN by default; a real broadcast needs ``confirm=True`` (→ ``CONFIRM_MAINNET_BROADCAST=yes``),
the second factor of the two-key interlock (SECURITY.md). The pure record→bundle-inputs transform
is kept import-light (no engine, no chain) so it unit-tests in isolation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

_HEX32_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")


class AgentdError(RuntimeError):
    """A lifecycle subprocess failed (non-zero exit, missing binary, or unparseable output)."""


def _validate_hash32(name: str, value: str | None, *, required: bool = False) -> str | None:
    """Require a 32-byte (64-hex-char) hash before it is folded into an on-chain commitment.

    chain_c's ``assertHash32`` only sees even-length hex; a truncated/typo'd value would land a
    permanent, unverifiable OP_RETURN on a real broadcast. Fail closed here and normalize to
    lowercase so the off-chain ``actionHash == receiptHash`` compare in bundle verification holds.
    """
    if value is None:
        if required:
            raise AgentdError(f"{name} is required (32-byte hex string, 64 hex chars)")
        return None
    if not isinstance(value, str) or not _HEX32_RE.match(value):
        raise AgentdError(f"{name} must be a 32-byte hex string (64 hex chars), got {value!r}")
    return value.lower()


def run_agentd(cmd: str, env_extra: dict, *, chain_c_dir: Path | str | None = None,
               agentd_bin: Path | str | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run one ``agentd`` subcommand (``deploy`` / ``action`` / ``revoke``).

    Returns the CompletedProcess; raises :class:`AgentdError` on a non-zero exit. The binary is run
    with ``cwd=chain_c`` so it resolves the committed AgentTea artifact and charter relative paths.
    """
    cdir = Path(chain_c_dir) if chain_c_dir else paths.chain_c_dir()
    binary = Path(agentd_bin) if agentd_bin else paths.chain_c_bin("agentd")
    if not binary.exists():
        raise AgentdError(
            f"chain_c agentd not found: {binary}\n"
            f"  build it: bash {cdir}/build_chain_c.sh   (-> $BONSAI_NOTARY_HOME/chain_c/build)")
    argv = [str(binary), cmd]
    env = {**os.environ, **{k: str(v) for k, v in env_extra.items() if v is not None}}
    # Make the AgentTea artifact + shared state home explicit (chain_c reads these by env).
    env.setdefault("AGENT_TEA_ARTIFACT", str(paths.agent_tea_artifact()))
    env.setdefault("BONSAI_AGENT_TEA_ARTIFACT", str(paths.agent_tea_artifact()))
    env.setdefault("BONSAI_NOTARY_HOME", str(paths.notary_home()))
    try:
        proc = subprocess.run(argv, cwd=str(cdir), env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise AgentdError(f"could not launch agentd ({binary}); is chain_c built?") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentdError(f"agentd {cmd} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise AgentdError(f"agentd {cmd} failed (exit {proc.returncode}):\n{proc.stderr.strip()}")
    return proc


_RECORD_KEYS = ("actionTxid", "receiptHashOnChain", "txCount", "lockTime", "amount",
                "actionHash", "provenanceHash", "identity")
_IDENTITY_KEYS = ("ricardianHash", "genesisTxid", "agentPubKey", "counterpartyPubKey")


def parse_action_record(stdout: str) -> dict | None:
    """Extract the last COMPLETE JSON action record from ``agentd action`` stdout.

    Returns None for dry-run output (no record) or a record missing any field that
    :func:`bundle_inputs_from_action_record` needs — so a malformed/truncated line never produces
    a half-built bundle that KeyErrors downstream.
    """
    for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or not all(k in obj for k in _RECORD_KEYS):
                continue
            ident = obj.get("identity")
            if not isinstance(ident, dict) or not all(k in ident for k in _IDENTITY_KEYS):
                continue
            # Key presence is not enough: amount/txCount/lockTime must be int-coercible, or the
            # downstream int() in bundle_inputs_from_action_record detonates with an uncaught
            # TypeError/ValueError on a key-complete-but-malformed record (review finding #23).
            try:
                int(obj["amount"]); int(obj["txCount"]); int(obj["lockTime"])
            except (TypeError, ValueError):
                continue
            return obj
    return None


def bundle_inputs_from_action_record(record: dict, network: str = "main") -> tuple[dict, dict]:
    """Turn an ``agentd action`` record into ``(onchain.json, identity.json)`` for ``pack_bundle``.

    The record binds the on-chain action to the Bonsai inference: ``actionHash`` is the
    ``receiptHash`` and ``provenanceHash`` is the ``modelHash`` (asserted again at offline verify).
    """
    ident = record["identity"]
    onchain = {
        "kind": "stateful",
        "network": network,
        "actionTxid": record["actionTxid"],
        "receiptVout": record.get("receiptVout", 1),
        "receiptHashOnChain": record["receiptHashOnChain"],
        "rawTx": record.get("rawTx"),
        "sizeBytes": record.get("sizeBytes"),
        "action": {
            "amount": int(record["amount"]),
            "txCount": int(record["txCount"]),
            "lockTime": int(record["lockTime"]),
            "actionHash": record["actionHash"],
            "provenanceHash": record["provenanceHash"],
        },
    }
    identity = {
        "ricardianHash": ident["ricardianHash"],
        "genesisTxid": ident["genesisTxid"],
        "agentPubKey": ident["agentPubKey"],
        "counterpartyPubKey": ident["counterpartyPubKey"],
    }
    return onchain, identity


@dataclass
class ChainCAgentd:
    """A handle to one persisted agent identity (its STATE_FILE) and the keys/env to operate it,
    driven through the chain_c ``agentd`` binary."""

    state_file: str | Path
    network: str = "main"
    chain_c_dir: Path | str | None = None
    agentd_bin: Path | str | None = None
    elder_key_file: str | None = None
    agent_key_file: str | None = None
    counterparty_key_file: str | None = None
    fund_deploy_key_file: str | None = None
    fund_action_key_file: str | None = None
    change_address: str | None = None
    confirm: bool = False
    timeout: int = 600
    extra_env: dict = field(default_factory=dict)

    def _base_env(self) -> dict:
        return {
            "STATE_FILE": str(self.state_file),
            "NETWORK": self.network,
            "ELDER_KEY_FILE": self.elder_key_file,
            "AGENT_KEY_FILE": self.agent_key_file,
            "COUNTERPARTY_KEY_FILE": self.counterparty_key_file,
            "FUND_DEPLOY_KEY_FILE": self.fund_deploy_key_file,
            "FUND_ACTION_KEY_FILE": self.fund_action_key_file,
            "CHANGE_ADDRESS": self.change_address,
            "CONFIRM_MAINNET_BROADCAST": "yes" if self.confirm else "no",
            **self.extra_env,
        }

    def _run(self, cmd: str, env_extra: dict) -> subprocess.CompletedProcess:
        return run_agentd(cmd, {**self._base_env(), **env_extra},
                          chain_c_dir=self.chain_c_dir, agentd_bin=self.agentd_bin, timeout=self.timeout)

    def deploy(self, *, ricardian_hash: str | None = None) -> subprocess.CompletedProcess:
        """Deploy the identity (writes STATE_FILE on a real broadcast). RICARDIAN_HASH binds the charter."""
        rid = _validate_hash32("ricardian_hash", ricardian_hash)
        return self._run("deploy", {"RICARDIAN_HASH": rid})

    def action(self, *, action_hash: str, provenance_hash: str = "00" * 32,
               amount: int = 1000) -> tuple[subprocess.CompletedProcess, dict | None]:
        """Run one metered action under the identity. action_hash = Bonsai receiptHash;
        provenance_hash = Bonsai modelHash. Returns ``(proc, action_record-or-None)``."""
        ah = _validate_hash32("action_hash", action_hash, required=True)
        ph = _validate_hash32("provenance_hash", provenance_hash, required=True)
        proc = self._run("action", {"ACTION_HASH": ah, "PROVENANCE_HASH": ph, "AMOUNT": str(amount)})
        return proc, parse_action_record(proc.stdout)

    def revoke(self) -> subprocess.CompletedProcess:
        """Elder kill-switch — dissolve the identity."""
        return self._run("revoke", {})

    def status(self) -> dict:
        """Read the persisted identity state directly (no binary needed)."""
        p = Path(self.state_file)
        if not p.exists():
            return {"status": "absent", "stateFile": str(p)}
        st = json.loads(p.read_text("utf-8"))
        return {"status": st.get("status"), "genesisTxid": st.get("genesisTxid"),
                "tip": (st.get("tip") or {}).get("txid"), "txCount": (st.get("state") or {}).get("txCount"),
                "ricardianHash": st.get("ricardianHash"), "stateFile": str(p)}
