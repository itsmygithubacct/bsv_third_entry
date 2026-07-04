# Contributing

Thanks for your interest in `bsv_third_entry`.

`bsv_third_entry` is the on-chain orchestration layer: it builds the environment
the `chain_c` CLIs read and drives the AgentTea identity lifecycle. It holds no
keys and does not talk to the network itself.

## Ground rules

- **Real money — fail closed.** Every on-chain action is **DRY-RUN by default**. A
  real broadcast requires an explicit `confirm=True` / `--confirm` at this layer,
  which is the *only* thing that sets the `chain_c` binary's own
  `CONFIRM_MAINNET_BROADCAST=yes` gate. Never weaken or auto-set that second factor.
  See [`SECURITY.md`](SECURITY.md).
- **Validate before you anchor.** `receiptHash` / `modelHash` / `ricardianHash` must
  be checked as 32-byte (64-hex) values before they reach the chain; a malformed
  hash must raise, not be committed as a permanent, unverifiable OP_RETURN.
- **Never leak secrets.** No key files, WIFs, mnemonics, funded addresses, or
  machine-specific paths in code, tests, or docs. Secrets live under
  `$BONSAI_NOTARY_HOME`, outside the repo.
- **Keep the transport transform pure.** The record→bundle-inputs transform is kept
  engine-/chain-free so it unit-tests in isolation; keep it that way.

## Developing and testing

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. python -m pytest tests/ -q          # offline; no keys, no network
```

- New behavior gets a test. The default test run must pass **offline** — no live
  network and no live keys.
- Keep changes focused; explain *why* in the commit body, not just *what*.

## License

By contributing you agree your contributions are licensed under the Apache
License 2.0 (see [`LICENSE`](LICENSE)).
