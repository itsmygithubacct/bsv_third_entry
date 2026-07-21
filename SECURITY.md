# Security Policy — bsv_third_entry

`bsv_third_entry` drives the `chain_c` CLIs to publish a receipt's Bitcoin SV Third Entry and to run
the AgentTea identity lifecycle. It builds the environment those CLIs read; it does not itself hold
keys or talk to the network.

## The two-key interlock (real-money safety)

Every on-chain action is **DRY-RUN by default**. A real broadcast that spends BSV requires **both**:

1. an explicit `confirm=True` / `--confirm` at this layer, **and**
2. the `chain_c` binary's own `CONFIRM_MAINNET_BROADCAST=yes` gate — which this layer sets *only* when
   (1) is given.

Never weaken or auto-set the second factor. In resumable mode, a `--confirm` request with no deployed
identity **fails closed** (it does not silently broadcast).

## Commitment-hash validation (fail-closed)

`receiptHash` / `modelHash` / `ricardianHash` are validated as 32-byte (64-hex) values **before** they
are handed to the chain. A truncated or malformed hash raises rather than being anchored as a
permanent, unverifiable commitment. Direct AgentTea actions require both `--action-hash` and
`--provenance-hash`; all-zero provenance is rejected. Receipt publication requires a nonzero
`modelHash`, and an explicit `provenanceHash` must match it—there is no receipt-hash fallback.

## Secrets

This package holds no secrets. Key files (`{wif,address}` JSON) live under
`$BONSAI_NOTARY_HOME/chain/` (default `~/.local/trinote/chain/`) and are read by the `chain_c` binaries,
never by this code. Do not commit key files, WIFs, or mnemonics; do not copy or ship
`$BONSAI_NOTARY_HOME`.

## Reporting a vulnerability

Report privately anything that could cause an unintended broadcast, a weakened interlock, or key
exposure — via GitHub Security Advisories ("Report a vulnerability" on this repository's **Security**
tab, or the repo-relative [`security/advisories/new`](../../security/advisories/new)). Do **not** open
a public issue for an unfixed vulnerability, and never include real key material in a report.
