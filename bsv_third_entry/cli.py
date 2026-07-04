"""``bsv-third-entry`` — publish a BSV Third Entry for a receipt via chain_c (DRY-RUN by default).

Engine-independent entrypoint: give it a chain-artifact JSON (the ``{receiptHash, modelHash, …}``
the engine emits) or the hashes directly, and it drives chain_c.

Default mode is **resumable** — one metered ``agentd action`` under a persisted identity. Deploy the
identity once (``bsv-agent deploy --confirm``); thereafter each Third Entry is a cheap state
transition. Use ``--oneshot`` for the self-contained ``bonsai_third_entry`` lifecycle (no persisted
identity, ephemeral keys).

  bsv-third-entry --receipt-hash <64hex> [--model-hash <64hex>]      # resumable, DRY-RUN
  bsv-third-entry --artifact chain-artifact.json --confirm           # real broadcast (spends BSV)
  bsv-third-entry --oneshot --artifact chain-artifact.json           # one-shot lifecycle, DRY-RUN
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .chain_backends import ChainCBroadcastError, ChainCThirdEntryBackend

_REPORT_KEYS = ("backend", "status", "broadcast", "identity", "txid", "network",
                "actionHash", "provenanceHash", "ricardianHash", "stateFile", "reason")


def main(argv=None) -> int:
    # allow_abbrev=False: the bash launchers gate auto-funding + fresh-change rotation on a LITERAL
    # ' --confirm '/' --chain-confirm ' substring, so a prefix like --conf must NOT resolve to
    # --confirm here — otherwise an abbreviation would trigger a real broadcast while the launcher
    # skipped change-address hygiene.
    ap = argparse.ArgumentParser(prog="bsv-third-entry", description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artifact", help="path to a chain-artifact JSON (with receiptHash/modelHash)")
    ap.add_argument("--receipt-hash", help="the Bonsai receiptHash (32-byte hex) → ACTION_HASH")
    ap.add_argument("--model-hash", help="the Bonsai modelHash (32-byte hex) → PROVENANCE_HASH")
    ap.add_argument("--provenance-hash", help="override PROVENANCE_HASH explicitly")
    ap.add_argument("--ricardian-hash", help="override RICARDIAN_HASH (else chain_c's charter default)")
    ap.add_argument("--confirm", action="store_true",
                    help="actually broadcast to mainnet (real BSV); omit for DRY-RUN")
    ap.add_argument("--oneshot", action="store_true",
                    help="use the self-contained bonsai_third_entry lifecycle instead of resumable agentd action")
    ap.add_argument("--state-file", default=None,
                    help="resumable identity STATE_FILE (default $BONSAI_NOTARY_HOME/agent/identity.state.json)")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--plan", action="store_true", help="also print the raw chain_c plan to stderr")
    args = ap.parse_args(argv)

    if args.artifact:
        try:
            artifact = json.loads(Path(args.artifact).read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            ap.error(f"--artifact unreadable or not valid JSON: {exc}")   # clean exit-2, not a traceback (review-2 #13)
        if not isinstance(artifact, dict):
            ap.error(f"--artifact must be a JSON object, got {type(artifact).__name__}")
    elif args.receipt_hash:
        artifact = {"receiptHash": args.receipt_hash}
        if args.model_hash:
            artifact["modelHash"] = args.model_hash
    else:
        ap.error("need --artifact or --receipt-hash")
    if args.provenance_hash:
        artifact["provenanceHash"] = args.provenance_hash

    backend = ChainCThirdEntryBackend(confirm=args.confirm, timeout=args.timeout,
                                      mode="oneshot" if args.oneshot else "resumable",
                                      state_file=args.state_file, ricardian_hash=args.ricardian_hash)
    try:
        record = backend.broadcast(artifact)
    except ChainCBroadcastError as exc:
        sys.stderr.write(f"[bsv-third-entry] {exc}\n")
        return 1

    if args.plan and record.get("plan"):
        sys.stderr.write(record["plan"] + "\n")
    print(json.dumps({k: record[k] for k in _REPORT_KEYS if k in record}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
