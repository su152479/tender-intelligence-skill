---
name: tender-intelligence-skill
description: Run, diagnose, and extend a Python tender-intelligence radar that scans public procurement notices, stores auditable notice and project facts in SQLite, and evaluates precast-product demand evidence. Use for engineering opportunity discovery, collector health checks, notice analysis, project timelines, reports, and regression evaluation.
---

# Tender Intelligence Skill

Work from this repository's root and read `README.md` before setup or operation.

## Operating workflow

1. Copy `.env.example` to `.env`; keep credentials, browser state, databases, downloads, logs, and reports out of source control.
2. Install with `pip install -e ".[dev]"` and install Chromium only when a Playwright-backed source is needed.
3. Initialize with `radar init`. Use `radar run --mock` before exercising real sources in a new environment.
4. Run selected sources explicitly, then inspect `radar status`. Judge a zero-result run from its raw-list funnel and health state rather than the final opportunity count alone.
5. For sources requiring authentication, use `radar login <source-id>` and let the user complete login or CAPTCHA. Never bypass CAPTCHA, WAF, rate limits, or access controls.
6. Run `pytest` and `radar evaluate` after changes that can affect collection, analysis, persistence, or reporting.

## Opportunity semantics

Keep these meanings separate:

- Notice actionability: whether the current notice can still be acted on.
- Project tracking: whether the underlying engineering project remains worth following.
- Product opportunity: whether a target product has demand evidence and an open or upcoming procurement window.

Do not treat project keywords as direct product procurement. Direct evidence requires a target product to be governed by an explicit procurement, supply, production, prefabrication, processing, or installation action. A closed construction tender may still be a valuable project-progress signal and supply-chain lead.

Treat `项目线索分` and `当前商机分` as explainable rule scores, not calibrated probabilities. Inspect the versioned `notice_product_assessment` record and its demand evidence, scope, procurement window, supporting sentences, and negative evidence before presenting a commercial conclusion.

## Data and project facts

- `project` remains the notice table; do not casually rename it or reinterpret its IDs.
- `notice_observation` records which real source run observed a notice and supplies the `NEW / UPDATED / SEEN_AGAIN` daily semantics.
- `engineering_project`, `project_notice_link`, identity facts, project events, and lifecycle aggregation represent increasingly derived project knowledge. Preserve evidence provenance and do not turn candidate relations into confirmed links.
- Lifecycle output is read-only and evidence-derived. Do not write inferred stages back unless a later task explicitly introduces a reviewed persistence design.
- Product opportunity, project tracking, and lifecycle are independent; never make one overwrite another as a shortcut.

## Safe maintenance

Keep source-specific selectors and request behavior inside the corresponding collector, product rules in `config/products.yaml`, and source metadata in `config/sources.yaml`.

Before changing product rules, add representative positive and difficult-negative fixtures to `tests/fixtures/opportunity_cases.json`. Before rewriting historical analysis, run `radar reanalyze` as a dry run; use `--apply` only with explicit authorization because it changes stored results.

Attachment enrichment may download only allowed official documents. Stop a source on CAPTCHA, HTTP 403, 418, or 429. Cleanup may remove expired local files only from the configured document directory while retaining parsed text, hashes, URLs, identifiers, and the FTS index.

Never publish or commit `.env`, API keys, account data, cookies, browser profiles, storage state, SQLite databases, attachment bodies, local paths, logs, diagnostics, reports, or website deployment configuration.
