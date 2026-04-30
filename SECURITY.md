# Security policy

## Reporting a vulnerability

If you find a security issue in this codebase:

1. **Do not open a public issue.** Open a private security advisory via GitHub: <https://github.com/hash02/hermes-kite/security/advisories/new>.
2. Include:
   - A short description of the issue and the affected code path (file + line range).
   - Reproduction steps. If a working PoC requires moving paper PnL or on-chain markers around, include the inputs verbatim.
   - Your assessment of impact: book corruption, leaked secret, on-chain tx manipulation, denial-of-service, etc.
3. We aim to acknowledge within 5 business days. Critical vulnerabilities get fix + advisory + CHANGELOG note within 14 days; non-critical within 30.

## Scope

In scope:
- Code under `engine/`, `funds/`, `scripts/`, `onchain/`, `tests/`.
- The settlement marker format and reconciliation logic.
- The agent passport DID hashing and verification.
- Dependency vulnerabilities surfaced by Dependabot.

Out of scope (these are by-design caveats, not bugs — file an issue for discussion instead of an advisory):
- Failures of upstream public APIs (DeFiLlama, Binance, Polymarket, CoinGecko, DexScreener, Pyth, Yahoo, Stooq, Superstate). Workers degrade gracefully on transient failures; permanent breakage is a feature change, not a vulnerability.
- The settlement marker tx is *not* a real DeFi transaction. It commits a content hash to chain — that's the integrity property. The system makes no claim of asset custody; positions are paper.
- The Kite testnet wallet (`0xA29fF03ABfd219e3c76D1C18653297B8201B7748`) holds testnet KITE only. Compromise of the test key affects no real capital.

## Secret management

The only sensitive configuration is the Kite signing key:

- **Storage**: env var `KITE_PRIVATE_KEY`. Never check in. `.gitignore` excludes `.env`, `*.key`, `wallet.json`.
- **Rotation**: when rotating, register a new agent passport (`onchain/register_agent.py`) on the new key, append the new tx to `data/agent_registry.json` keeping the prior passport tx in history, then update the runner's env. Old key remains valid for read access; do not destroy until reconciliation is clean on the new key for ≥7 cron cycles. See `docs/runbooks/settlement-nonce-mismatch.md` for the recovery flow.
- **CI**: GitHub Actions never has access to `KITE_PRIVATE_KEY`. CI runs `scripts/reconcile.py --skip-onchain` (book-only); on-chain checks are an operator's local concern.

## No-secrets-in-logs

`engine/logging_setup.py` emits structured JSON. Workers must never log:
- Private keys (none should ever be in scope of a worker — only the executor handles them).
- Full env dumps (write narrow, named fields).
- Raw HTTP responses from feeds with auth headers (none of our feeds use auth, but if you add one with a key — strip it).

When in doubt, log `exc_info=True` for stack traces and a structured `extra={"category": ...}` for context, not the raw object.

## Dependency hygiene

- `pyproject.toml` pins `web3==6.20.0` (the on-chain executor needs the exact API surface we tested against). Floor-pins (`requests>=2.31`, `python-dotenv>=1.0`) elsewhere.
- `.github/dependabot.yml` opens weekly PRs for `pip` and `github-actions` ecosystems.
- Pre-commit's `check-added-large-files` blocks files >200KB to prevent accidental binary blob commits.

## Known-good baselines

If you suspect the repo has been tampered with:

```bash
python scripts/reconcile.py --skip-onchain     # passport hash + book integrity
python -m unittest discover tests              # 77/77 should pass
ruff check && ruff format --check .            # zero output on clean repo
mypy engine                                    # zero errors
```

Any failure here means: investigate before merging anything.
