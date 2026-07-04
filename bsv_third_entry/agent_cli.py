"""CLI for the resumable AgentTea lifecycle, driven through chain_c ``agentd``.

A thin argparse front-end over :class:`notary.agentd.ChainCAgentd`. DRY-RUN by default; a real
broadcast needs ``--confirm`` (→ ``CONFIRM_MAINNET_BROADCAST=yes``). Keys/state live under the
shared ``$BONSAI_NOTARY_HOME`` (default ``~/.local/trinote``).

  bonsai-agent status                      # read persisted identity state (no chain)
  bonsai-agent deploy  --ricardian-hash H  # deploy the identity (DRY-RUN unless --confirm)
  bonsai-agent action  --action-hash R --provenance-hash M
  bonsai-agent revoke
  bonsai-agent recover --new-agent-key-file F  # M-of-3 social recovery: rotate the agent key
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import paths
from .agentd import AgentdError, ChainCAgentd


def _default_state_file() -> str:
    return str(paths.notary_home() / "agent" / "identity.state.json")


def _build(args) -> ChainCAgentd:
    return ChainCAgentd(
        state_file=args.state_file,
        network=args.network,
        elder_key_file=args.elder_key_file,
        agent_key_file=args.agent_key_file,
        counterparty_key_file=args.counterparty_key_file,
        fund_deploy_key_file=args.fund_deploy_key_file,
        fund_action_key_file=args.fund_action_key_file,
        change_address=args.change_address,
        confirm=args.confirm,
    )


def main(argv=None) -> int:
    # allow_abbrev=False: the bash launchers gate auto-funding + fresh-change rotation on a LITERAL
    # ' --confirm ' substring, so a prefix like --conf must NOT resolve to --confirm here — otherwise
    # an abbreviation would trigger a real broadcast while the launcher skipped change-address hygiene.
    ap = argparse.ArgumentParser(prog="bsv-agent", description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "deploy", "action", "revoke", "recover"])
    ap.add_argument("--state-file", default=_default_state_file())
    ap.add_argument("--network", default="main", choices=["main", "test"])
    ap.add_argument("--confirm", action="store_true",
                    help="actually broadcast to mainnet (real BSV); omit for DRY-RUN")
    ap.add_argument("--ricardian-hash", default=None)
    ap.add_argument("--action-hash", default=None, help="the Bonsai receiptHash (required for 'action')")
    ap.add_argument("--provenance-hash", default="00" * 32, help="the Bonsai modelHash")
    ap.add_argument("--amount", type=int, default=1000)
    for kf in ("elder", "agent", "counterparty", "fund-deploy", "fund-action"):
        ap.add_argument(f"--{kf}-key-file", dest=kf.replace("-", "_") + "_key_file", default=None)
    ap.add_argument("--change-address", default=None)
    # recover (M-of-3 social recovery): rotate the agent key to a fresh keyfile. NEW_AGENT_KEY_FILE is
    # required + validated below; --fund-key-file → FUND_RECOVER_KEY_FILE (agentd defaults it to the
    # action fund key). Guardian sigs come from --recover-sigs-file OR the persisted recovery keys.
    ap.add_argument("--new-agent-key-file", dest="new_agent_key_file", default=None,
                    help="rotated-to agent keyfile ({wif,address} JSON); required for 'recover'")
    ap.add_argument("--recover-sigs-file", dest="recover_sigs_file", default=None,
                    help="operator-supplied guardian sigs (3 lines '<used 0|1> <s_hex|-> <padding>')")
    ap.add_argument("--fund-key-file", dest="fund_key_file", default=None,
                    help="keyfile that funds the recover fee (→ FUND_RECOVER_KEY_FILE)")
    args = ap.parse_args(argv)

    Path(args.state_file).parent.mkdir(parents=True, exist_ok=True)
    agent = _build(args)

    try:
        if args.command == "status":
            print(json.dumps(agent.status(), indent=2))
            return 0
        if args.command == "deploy":
            proc = agent.deploy(ricardian_hash=args.ricardian_hash)
            sys.stdout.write(proc.stdout)
            return 0
        if args.command == "action":
            if not args.action_hash:
                ap.error("'action' requires --action-hash (the Bonsai receiptHash)")
            proc, record = agent.action(action_hash=args.action_hash,
                                        provenance_hash=args.provenance_hash, amount=args.amount)
            sys.stdout.write(proc.stdout)
            if record:
                sys.stderr.write("\n[bonsai-agent] parsed action record (bundle-ready)\n")
            return 0
        if args.command == "revoke":
            proc = agent.revoke()
            sys.stdout.write(proc.stdout)
            return 0
        if args.command == "recover":
            if not args.new_agent_key_file:
                ap.error("'recover' requires --new-agent-key-file (the rotated-to agent keyfile)")
            new_agent = Path(args.new_agent_key_file)
            if not new_agent.is_file():
                ap.error(f"--new-agent-key-file not found: {args.new_agent_key_file}")
            # Reuse the dataclass runner: _base_env() supplies STATE_FILE/NETWORK/ELDER_KEY_FILE and the
            # CONFIRM_MAINNET_BROADCAST two-key gate (set only when --confirm); the recover-only vars ride
            # in as env_extra. We forward the keyfile PATH only — never its WIF contents.
            proc = agent._run("recover", {
                "NEW_AGENT_KEY_FILE": str(new_agent),
                "RECOVER_SIGS_FILE": args.recover_sigs_file,
                "FUND_RECOVER_KEY_FILE": args.fund_key_file,
            })
            sys.stdout.write(proc.stdout)
            return 0
    except AgentdError as exc:
        sys.stderr.write(f"[bonsai-agent] {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
