# bsv_third_entry

**Agent / on-chain orchestration for deterministic-inference receipts.** This project turns a
Bonsai inference *receipt* into a Bitcoin SV **Third Entry** by driving the
[`chain_c`](https://github.com/itsmygithubacct/chain_c) C CLIs — it is the Python layer that sits between a receipt and the chain.

It is engine-agnostic: it needs only a built `chain_c` checkout and the receipt's commitment
hashes. It is consumed by the larger [`bonsai-notary`](https://github.com/itsmygithubacct/bonsai-notary) composition, but runs
standalone too.

## Get it

```bash
git clone https://github.com/itsmygithubacct/bsv_third_entry.git
cd bsv_third_entry
git clone https://github.com/itsmygithubacct/chain_c.git ../chain_c   # its one dependency
ln -sfn ../chain_c chain_c                                            # CLIs build under chain_c/build
bash chain_c/build_chain_c.sh                                         # build the C CLIs (see chain_c/README)
```

`chain_c` is the only dependency. The `./chain_c` path is env-overridable via `BONSAI_CHAIN_C_DIR`
(it also falls back to `~/chain_c`) — point it at an existing checkout instead if you have one.

## What "Third Entry" means

A notarized generation carries a triple-entry receipt:

1. **First entry** — the model signs `(modelHash, inputCommit, outputCommit, traceCommit)`.
2. **Second entry** — the counterparty co-signs what it received.
3. **Third entry** — an **on-chain** commitment binding the receipt to Bitcoin SV, which the model
   cannot delete or reorder. *This package publishes that third entry.*

Two flavours, both via `chain_c` — **resumable is the default**:

| Flavour | chain_c CLI | This package |
|---|---|---|
| **Resumable** (default) — one metered action under a **persisted, pre-deployed** identity; a stream of receipts binds to one reputation-bearing agent | `agentd action` | `ChainCThirdEntryBackend` / `bsv-third-entry`, `ChainCAgentd` / `bsv-agent` |
| One-shot (`--oneshot`) — self-contained deploy→executeAction→revoke with ephemeral keys; no persisted identity | `bonsai_third_entry` | `ChainCThirdEntryBackend(mode="oneshot")` / `bsv-third-entry --oneshot` |

Resumable means: deploy the identity **once** (`bsv-agent deploy --confirm`), then every receipt is a
cheap `agentd action` state transition — no per-receipt deploy/revoke. Until an identity is deployed,
a resumable DRY-RUN reports `identity: absent` with a "deploy first" hint, and a real (`--confirm`)
broadcast fails closed rather than spending.

## Layout

```
bsv_third_entry/
  paths.py           resolve chain_c dir, $BONSAI_NOTARY_HOME, the AgentTea artifact (all env-overridable)
  _parse.py          parsers for the chain_c CLIs' human-readable stdout
  chain_backends.py  ChainCThirdEntryBackend — drop-in for trinote's WalletThirdEntryBackend
  agentd.py          ChainCAgentd — resumable AgentTea lifecycle + action-record→bundle transform
  agent_cli.py       `bsv-agent` CLI (deploy / action / revoke / status)
  cli.py             `bsv-third-entry` CLI (publish a Third Entry from a receipt's hashes/artifact)
  engine_run.py      bridge: run a trinote engine's run_bonsai_cli with this backend as --onchain
bsv-third-entry      launcher -> bsv_third_entry.cli
bsv-agent            launcher -> bsv_third_entry.agent_cli
chain_c -> ../chain_c (symlink; the built CLIs live under chain_c/build)
tests/               pure-stdlib unit tests + a real DRY-RUN integration smoke
```

## The two-key interlock (safety)

Everything is **DRY-RUN by default**. A real broadcast that spends BSV needs **both**:

1. an explicit `confirm=True` / `--confirm` at this layer, **and**
2. the chain_c binary's own `CONFIRM_MAINNET_BROADCAST=yes` gate (this layer sets it only when (1) holds).

Commitment hashes are validated as 32-byte hex **before** they are handed to the chain (fail-closed),
so a truncated/typo'd hash can never be anchored as a permanent, unverifiable OP_RETURN.

## Use

Prereqs: a built `chain_c` (`bash chain_c/build_chain_c.sh`) and an Elder
key file at `$BONSAI_NOTARY_HOME/chain/test_bsv.json` (default home `~/.local/trinote`).

```bash
# 0. deploy the resumable identity ONCE (DRY-RUN unless --confirm; --confirm spends BSV):
./bsv-agent deploy --ricardian-hash <64hex> --confirm        # writes the STATE_FILE

# 1. publish a Third Entry for a receipt — resumable agentd action (DRY-RUN by default):
./bsv-third-entry --receipt-hash <64hex> --model-hash <64hex>
./bsv-third-entry --artifact chain-artifact.json --plan      # from the engine's chain-artifact JSON
./bsv-third-entry --artifact chain-artifact.json --confirm   # real broadcast (needs the deployed identity)

# one-shot escape hatch (no persisted identity; ephemeral keys):
./bsv-third-entry --oneshot --artifact chain-artifact.json

# inspect / drive the identity directly:
./bsv-agent status
./bsv-agent action --action-hash <receiptHash> --provenance-hash <modelHash>
./bsv-agent revoke --confirm
```

### As an inference `--onchain` backend

`engine_run` rebinds an `trinote` engine's `--onchain` publisher to `ChainCThirdEntryBackend` with
no change to the engine source:

```bash
BONSAI_ENGINE_DIR=~/integer_inference_engine \
  PYTHONPATH=. python3 -m bsv_third_entry.engine_run --fast --receipt --onchain -p "Hello." -n 16
```

## Config (env)

| Var | Meaning | Default |
|---|---|---|
| `BONSAI_CHAIN_C_DIR` | chain_c checkout (CLIs under `<dir>/build`) | `./chain_c`, else `~/chain_c` |
| `BONSAI_NOTARY_HOME` | shared state/secrets home (chain key files live here) | `~/.local/trinote` |
| `BONSAI_AGENT_TEA_ARTIFACT` | committed AgentTea artifact | `<chain_c>/artifacts/src/contracts-next/agentTea.json` |
| `BONSAI_ENGINE_DIR` | (engine_run only) the `trinote` engine checkout | — |

## Test

```bash
uv venv && uv pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q
```

The integration smoke runs the real `bonsai_third_entry` in DRY-RUN (never broadcasts); it skips
automatically if chain_c is not built or no key file is present.
