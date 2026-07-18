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

import hashlib
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
_ACTION_PLAN_RE = re.compile(
    r"^action plan: txCount ([0-9]+) amount ([0-9]+) actionHash ([0-9a-fA-F]{64})$",
    re.MULTILINE,
)
_SIGNED_RAW_TX_RE = re.compile(r"^signed raw tx: ([0-9a-fA-F]+)$", re.MULTILINE)


class ActionRecordRecoveryError(ValueError):
    """A legacy flattened action emission could not be proven equivalent to a full record."""


def _hex_bytes(name: str, value: object, size: int) -> bytes:
    try:
        raw = bytes.fromhex(str(value))
    except ValueError as exc:
        raise ActionRecordRecoveryError(f"{name} is not hex") from exc
    if len(raw) != size:
        raise ActionRecordRecoveryError(f"{name} must be {size} bytes")
    return raw


def _compact_size(raw: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(raw):
        raise ActionRecordRecoveryError("raw transaction is truncated")
    first = raw[offset]
    offset += 1
    if first < 0xfd:
        return first, offset
    width = {0xfd: 2, 0xfe: 4, 0xff: 8}[first]
    end = offset + width
    if end > len(raw):
        raise ActionRecordRecoveryError("raw transaction has a truncated CompactSize")
    return int.from_bytes(raw[offset:end], "little"), end


def _take(raw: bytes, offset: int, size: int) -> tuple[bytes, int]:
    end = offset + size
    if size < 0 or end > len(raw):
        raise ActionRecordRecoveryError("raw transaction is truncated")
    return raw[offset:end], end


def _tx_inputs_outputs_locktime(raw_hex: str) -> tuple[list[dict], list[bytes], int]:
    """Parse the legacy BSV transaction fields needed to authenticate an action record."""
    try:
        raw = bytes.fromhex(raw_hex)
    except ValueError as exc:
        raise ActionRecordRecoveryError("signed raw transaction is not hex") from exc
    if len(raw) < 10:
        raise ActionRecordRecoveryError("signed raw transaction is too short")
    offset = 4  # version
    n_inputs, offset = _compact_size(raw, offset)
    inputs: list[dict] = []
    for _ in range(n_inputs):
        prev, offset = _take(raw, offset, 32)
        vout_raw, offset = _take(raw, offset, 4)
        script_len, offset = _compact_size(raw, offset)
        _, offset = _take(raw, offset, script_len)
        _, offset = _take(raw, offset, 4)  # sequence
        inputs.append({"prevTxid": prev[::-1].hex(), "vout": int.from_bytes(vout_raw, "little")})
    n_outputs, offset = _compact_size(raw, offset)
    outputs: list[bytes] = []
    for _ in range(n_outputs):
        _, offset = _take(raw, offset, 8)  # satoshis
        script_len, offset = _compact_size(raw, offset)
        script, offset = _take(raw, offset, script_len)
        outputs.append(script)
    lock_raw, offset = _take(raw, offset, 4)
    if offset != len(raw):
        raise ActionRecordRecoveryError("raw transaction has trailing bytes")
    return inputs, outputs, int.from_bytes(lock_raw, "little")


def _op_return_items(script: bytes) -> list[bytes] | None:
    offset = 1 if script.startswith(b"\x00") else 0
    if offset >= len(script) or script[offset] != 0x6A:
        return None
    offset += 1
    items: list[bytes] = []
    while offset < len(script):
        op = script[offset]
        offset += 1
        if op < 0x4C:
            size = op
        elif op == 0x4C:
            size_raw, offset = _take(script, offset, 1)
            size = size_raw[0]
        elif op == 0x4D:
            size_raw, offset = _take(script, offset, 2)
            size = int.from_bytes(size_raw, "little")
        elif op == 0x4E:
            size_raw, offset = _take(script, offset, 4)
            size = int.from_bytes(size_raw, "little")
        else:
            raise ActionRecordRecoveryError("OP_RETURN contains a non-push opcode")
        item, offset = _take(script, offset, size)
        items.append(item)
    return items


def _receipt_mark(*, identity: dict, amount: int, action_hash: str,
                  provenance_hash: str, tx_count: int, lock_time: int) -> str:
    for name, value, width in (("amount", amount, 8), ("txCount", tx_count, 8),
                               ("lockTime", lock_time, 4)):
        if value < 0 or value >= (1 << (width * 8 - 1)):
            raise ActionRecordRecoveryError(f"{name} is outside the AgentTea encoding range")
    preimage = (
        _hex_bytes("ricardianHash", identity.get("ricardianHash"), 32)
        + _hex_bytes("agentPubKey", identity.get("agentPubKey"), 33)
        + _hex_bytes("counterpartyPubKey", identity.get("counterpartyPubKey"), 33)
        + amount.to_bytes(8, "little")
        + _hex_bytes("actionHash", action_hash, 32)
        + _hex_bytes("provenanceHash", provenance_hash, 32)
        + tx_count.to_bytes(8, "little")
        + lock_time.to_bytes(4, "little")
    )
    return hashlib.sha256(preimage).hexdigest()


def recover_action_record(stdout: str, *, state_file: str | Path, pre_state: dict,
                          action_txid: str, expected_action_hash: str,
                          expected_provenance_hash: str, expected_amount: int) -> dict:
    """Prove and recover a full action record from legacy chain_c's flattened live output.

    Older ``agentd`` builds print an action plan, signed raw transaction and broadcast txid but no
    final JSON object.  This transform is deliberately strict: it binds the pre/post state files to
    input[0]/the new tip, recomputes the txid and AgentTea mark, and only then returns bundle inputs.
    """
    plans = _ACTION_PLAN_RE.findall(stdout)
    raw_txs = _SIGNED_RAW_TX_RE.findall(stdout)
    if len(plans) != 1 or len(raw_txs) != 1:
        raise ActionRecordRecoveryError("expected exactly one action plan and one signed raw transaction")
    tx_count_s, amount_s, action_hash = plans[0]
    tx_count, amount = int(tx_count_s), int(amount_s)
    action_hash = action_hash.lower()
    provenance_hash = expected_provenance_hash.lower()
    raw_tx = raw_txs[0].lower()
    action_txid = action_txid.lower()
    if action_hash != expected_action_hash.lower() or amount != int(expected_amount):
        raise ActionRecordRecoveryError("action plan disagrees with the requested receipt or amount")
    try:
        raw = bytes.fromhex(raw_tx)
    except ValueError as exc:
        raise ActionRecordRecoveryError("signed raw transaction is not hex") from exc
    computed_txid = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()
    if computed_txid != action_txid:
        raise ActionRecordRecoveryError("signed raw transaction does not hash to the broadcast txid")

    try:
        post_state = json.loads(Path(state_file).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ActionRecordRecoveryError("cannot read the post-action identity state") from exc
    try:
        pre_count = int(pre_state["state"]["txCount"])
        post_count = int(post_state["state"]["txCount"])
        pre_tip = pre_state["tip"]
        post_tip = post_state["tip"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ActionRecordRecoveryError("identity state is missing its counter or tip") from exc
    if tx_count != pre_count or post_count != pre_count + 1:
        raise ActionRecordRecoveryError("persisted AgentTea counter is not the planned one-step transition")
    if not isinstance(pre_tip, dict) or not isinstance(post_tip, dict):
        raise ActionRecordRecoveryError("identity state tip must be an object")
    if post_tip.get("txid") != action_txid or str(post_tip.get("rawTxHex", "")).lower() != raw_tx:
        raise ActionRecordRecoveryError("post-action AgentTea tip does not match the signed transaction")

    identity = {}
    for key in _IDENTITY_KEYS:
        before, after = pre_state.get(key), post_state.get(key)
        if before != after:
            raise ActionRecordRecoveryError(f"identity field {key} changed during executeAction")
        identity[key] = after
    _hex_bytes("ricardianHash", identity["ricardianHash"], 32)
    _hex_bytes("genesisTxid", identity["genesisTxid"], 32)
    _hex_bytes("agentPubKey", identity["agentPubKey"], 33)
    _hex_bytes("counterpartyPubKey", identity["counterpartyPubKey"], 33)

    inputs, outputs, lock_time = _tx_inputs_outputs_locktime(raw_tx)
    try:
        prior_vout = int(pre_tip.get("vout"))
    except (TypeError, ValueError) as exc:
        raise ActionRecordRecoveryError("prior AgentTea tip has no valid vout") from exc
    if not inputs or inputs[0].get("prevTxid") != pre_tip.get("txid") \
            or inputs[0].get("vout") != prior_vout:
        raise ActionRecordRecoveryError("action input[0] does not spend the prior AgentTea tip")
    anchors = [(vout, _op_return_items(script)) for vout, script in enumerate(outputs)]
    anchors = [(vout, items) for vout, items in anchors if items is not None]
    if len(anchors) != 1:
        raise ActionRecordRecoveryError("action must contain exactly one OP_RETURN output")
    receipt_vout, items = anchors[0]
    data = [item.hex() for item in items if item]
    if len(data) != 1 or len(data[0]) != 64:
        raise ActionRecordRecoveryError("action OP_RETURN must contain one 32-byte AgentTea mark")
    expected_mark = _receipt_mark(
        identity=identity, amount=amount, action_hash=action_hash,
        provenance_hash=provenance_hash, tx_count=tx_count, lock_time=lock_time,
    )
    if data[0] != expected_mark:
        raise ActionRecordRecoveryError("action OP_RETURN does not match the recomputed AgentTea mark")
    return {
        "actionTxid": action_txid,
        "receiptVout": receipt_vout,
        "receiptHashOnChain": data[0],
        "txCount": tx_count,
        "lockTime": lock_time,
        "amount": amount,
        "actionHash": action_hash,
        "provenanceHash": provenance_hash,
        "identity": identity,
        "rawTx": raw_tx,
        "sizeBytes": len(raw),
    }


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
