# Contributing

## Adding a wallet (via pull request)

1. Add one entry to `data/registry.json`. Do **not** edit the API
   or the sample entries in the same PR.
2. Every rugged mint needs:
   - `mint` (valid Solana address), `outcome: "rugged"`
   - `peak_market_cap_usd` and `current_market_cap_usd`
   - `lp_removed` (true/false), `dev_sell_pct`, `time_to_collapse_minutes`
   - `evidence` with at least one http(s) link per mint — a Solscan
     link to the mint, the dev wallet, and the LP-removal / dev-sell
     transaction. A mint without evidence is rejected by validation.
3. **At least 2 rugged mints** are required for `status: "flagged"`.
   With 1, use `status: "under_review"`.
4. Set `first_seen` / `last_seen` (YYYY-MM-DD) and `ttl_days`
   (default 180). Set `cluster_id` if the wallet is funding-linked
   to another entry; otherwise use a new `cluster-<…>` id.
5. Run `pytest -q` and confirm it passes before opening the PR.

## Disputes / appeals

Anyone can dispute an entry:

1. Open a PR (or issue) naming the wallet and the mint(s) you
   dispute, with your own evidence links (e.g. the launch was an
   honest failure, the wallet is a shared launchpad wallet, or the
   funding link is coincidental).
2. A maintainer marks the entry `status: "disputed"` while it is
   reviewed and records the dispute in `dispute_notes`.
3. If the dispute is upheld, the entry is removed or corrected in
   a follow-up PR. If it is rejected, the reason is recorded in
   `dispute_notes` and the entry stays.

Disputed entries are **not** returned as flagged by `/check`
(they return `risk_level: "watch"`).

## False-positive controls (why they exist)

- Launchpad / shared wallets can look like serial ruggers — check
  whether the wallet is a program/launchpad address before flagging.
- A failed-but-honest launch (big collapse, no LP removal, dev
  sold < 20%) must be labelled `failed_but_honest` in the labeller,
  not counted as a rug.
- Poisoning: a rugger could try to get an innocent wallet flagged.
  The ≥ 2 rule, per-mint evidence, TTL expiry and the public PR
  review process are the mitigations. Never flag on a single
  anonymous report.

## Code style

- Python 3.10+, type hints, pure functions where possible
  (labeller / clusterer must stay network-free and unit-testable).
- `pytest -q` must pass. Add a test for every new rule.
- No secrets in code or data. `.env` is git-ignored; use
  `.env.example` as the template.
