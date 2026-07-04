"""Tests for the bsv_third_entry on-chain orchestration.

Pure-stdlib + pytest: env/command construction, output parsers, fail-closed hash guard, and the
agentd record transform — none need an inference engine. Two integration smokes run the real chain_c
CLIs in DRY-RUN (never broadcast):

  * resumable ``agentd action`` against a crafted STATE_FILE — needs only the built ``agentd``
    (no keys, no network);
  * one-shot ``bonsai_third_entry`` — needs the built binary AND a key file (skips otherwise).

Run:  PYTHONPATH=. python3 -m pytest tests/ -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bsv_third_entry import _parse, agentd, paths  # noqa: E402
from bsv_third_entry.chain_backends import ChainCBroadcastError, ChainCThirdEntryBackend  # noqa: E402

SAMPLE_DRYRUN = """\
Network        : livenet
Elder          : 17is8hXCq66ZbRBajN6ehYSnhMBufBs5gz
ricardianHash  : c0d6bef23e7d8df9b64372e6510aeb68e3dbf48e6d5960391920e361b766fb6a
actionHash     : 1111111111111111111111111111111111111111111111111111111111111111

DRY RUN — not broadcasting. Set CONFIRM_MAINNET_BROADCAST=yes to run live.
"""

SAMPLE_LIVE = "BROADCAST OK: 99887766554433221100ffeeddccbbaa00112233445566778899aabbccddeeff\n"

R_HASH = "ab" * 32
M_HASH = "cd" * 32

DEPLOYED_STATE = json.dumps({
    "status": "deployed", "network": "main",
    "genesisTxid": "aa" * 32, "ricardianHash": "ef" * 32,
    "tip": {"txid": "aa" * 32, "vout": 1}, "state": {"txCount": "3"},
})


# --- parsers ---------------------------------------------------------------------------------

def test_field_and_dryrun_and_txid():
    assert _parse.field(SAMPLE_DRYRUN, "actionHash") == "11" * 32
    assert _parse.is_dry_run(SAMPLE_DRYRUN) is True
    assert _parse.is_dry_run(SAMPLE_LIVE) is False
    assert _parse.txid_after(SAMPLE_LIVE, "BROADCAST OK") == \
        "99887766554433221100ffeeddccbbaa00112233445566778899aabbccddeeff"


# --- hash binding / mode ---------------------------------------------------------------------

def test_default_mode_is_resumable():
    assert ChainCThirdEntryBackend().mode == "resumable"
    assert ChainCThirdEntryBackend().label == "chain_c:agentd-action"
    assert ChainCThirdEntryBackend(mode="oneshot").label == "chain_c:bonsai_third_entry"


def test_hashes_bind_receipt_and_model():
    be = ChainCThirdEntryBackend()
    a, p = be._hashes({"receiptHash": R_HASH, "modelHash": M_HASH})
    assert (a, p) == (R_HASH, M_HASH)                 # actionHash=receiptHash, provenanceHash=modelHash
    a, p = be._hashes({"receiptHash": R_HASH})
    assert p == R_HASH                                  # no modelHash → falls back to receiptHash


def test_hashes_reject_bad():
    be = ChainCThirdEntryBackend()
    with pytest.raises(ChainCBroadcastError):
        be._hashes({"receiptHash": "deadbeef"})
    with pytest.raises(ChainCBroadcastError):
        be._hashes({})


def test_oneshot_env_gate():
    be = ChainCThirdEntryBackend(confirm=False, mode="oneshot")
    env = be._oneshot_env({"receiptHash": R_HASH, "modelHash": M_HASH})
    assert env["ACTION_HASH"] == R_HASH and env["PROVENANCE_HASH"] == M_HASH
    assert "CONFIRM_MAINNET_BROADCAST" not in env
    env2 = ChainCThirdEntryBackend(confirm=True, mode="oneshot")._oneshot_env({"receiptHash": R_HASH})
    assert env2["CONFIRM_MAINNET_BROADCAST"] == "yes"


def test_accepts_wallet_backend_kwargs():
    be = ChainCThirdEntryBackend(source_index=23, sat_per_kb=100,
                                 change_to_source=True, allow_unconfirmed=True, confirm=False)
    assert be.confirm is False and be.mode == "resumable"


# --- resumable: no identity deployed yet -----------------------------------------------------

def test_resumable_absent_identity_dryruns_clean(tmp_path):
    be = ChainCThirdEntryBackend(confirm=False, state_file=tmp_path / "nope.json")
    out = be.broadcast({"receiptHash": R_HASH})
    assert out["status"] == "dry-run" and out["broadcast"] is False
    assert out["identity"] == "absent"
    assert "deploy" in out["reason"].lower()


def test_resumable_absent_identity_confirm_fails_closed(tmp_path):
    be = ChainCThirdEntryBackend(confirm=True, state_file=tmp_path / "nope.json")
    with pytest.raises(ChainCBroadcastError):       # never silently broadcasts without an identity
        be.broadcast({"receiptHash": R_HASH})


# --- agentd record transform -----------------------------------------------------------------

def test_parse_action_record_and_bundle_inputs():
    rec = {
        "actionTxid": "aa" * 32, "receiptHashOnChain": R_HASH, "txCount": 1, "lockTime": 0,
        "amount": 1000, "actionHash": R_HASH, "provenanceHash": M_HASH,
        "identity": {"ricardianHash": "ef" * 32, "genesisTxid": "bb" * 32,
                     "agentPubKey": "02" + "00" * 32, "counterpartyPubKey": "03" + "00" * 32},
    }
    assert agentd.parse_action_record(json.dumps(rec)) == rec
    onchain, identity = agentd.bundle_inputs_from_action_record(rec)
    assert onchain["action"]["actionHash"] == R_HASH
    assert identity["genesisTxid"] == "bb" * 32


def test_parse_action_record_rejects_incomplete():
    assert agentd.parse_action_record("DRY RUN — nothing broadcast") is None
    assert agentd.parse_action_record('{"actionTxid":"x"}') is None


# --- integration smokes (real chain_c, DRY-RUN, never broadcasts) ----------------------------

@pytest.mark.skipif(not paths.chain_c_bin("agentd").exists(), reason="chain_c not built")
def test_resumable_dryrun_against_deployed_state(tmp_path):
    """The default path: one metered agentd action under a (crafted) deployed identity. No keys/network."""
    state = tmp_path / "identity.state.json"
    state.write_text(DEPLOYED_STATE)
    be = ChainCThirdEntryBackend(confirm=False, state_file=state)   # resumable default
    out = be.broadcast({"receiptHash": R_HASH, "modelHash": M_HASH})
    assert out["status"] == "dry-run" and out["broadcast"] is False
    assert out["identity"] == "deployed"
    assert out["actionHash"] == R_HASH and out["provenanceHash"] == M_HASH
    assert out["txid"].startswith("dryrun:")
    assert "action plan" in out["plan"]


@pytest.mark.skipif(not paths.chain_c_bin("bonsai_third_entry").exists(), reason="chain_c not built")
@pytest.mark.skipif(not (paths.notary_home() / "chain" / "test_bsv.json").exists(),
                    reason="no chain key file under $BONSAI_NOTARY_HOME/chain")
def test_oneshot_dryrun(tmp_path):
    be = ChainCThirdEntryBackend(confirm=False, mode="oneshot")
    out = be.broadcast({"receiptHash": R_HASH, "modelHash": M_HASH})
    assert out["status"] == "dry-run" and out["broadcast"] is False
    assert out["actionHash"] == R_HASH
    assert out["txid"].startswith("dryrun:")
    assert "DRY RUN" in out["plan"]
