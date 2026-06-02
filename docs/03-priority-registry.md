# 03 — Priority Registry

The priority registry is a flat mapping from `source_id` (the corpus's unique identifier for a source — a Twitter handle, an RSS feed name, a domain, whatever you index by) to a categorical priority level: `high`, `normal`, or `low`. It is the only manual labour the system requires. Everything else derives from it automatically.

## Why three levels, not five or two

Two levels (`high` vs `not-high`) lose the ability to demote known-noisy sources. The system's behaviour around aggregator accounts, headline-summary bots, and recently-onboarded sources with no track record is materially better when those sources can be tagged `low` and pushed down rather than left at the default.

Five-or-more levels compound the tagging burden without measurable signal gain. Once you're at the level of distinguishing "good" from "very good," your inter-rater reliability against your own judgments six months ago will be poor. Three levels is the maximum granularity humans can reliably maintain on a moving corpus.

The three-level scheme also maps cleanly to a three-value multiplier in scoring:

| Priority | Multiplier (in `scoring.py`) |
|---|---|
| `high` | `1.5×` (production default; some deployments use `2.5×` for the deepest signal in expertise-heavy queries) |
| `normal` | `1.0×` |
| `low` | `0.5×` |

The `low` multiplier doesn't *remove* low-priority sources — it pushes them down by half. They remain reachable, which matters when a low-priority source happens to be the only one talking about a niche topic.

## Format

YAML, because the registry is read at startup, lives next to the rest of the configuration, and benefits from comments. A SQLite table is an equally valid choice when the registry is large enough to warrant indexed lookups (≥ ~5,000 sources) or when multiple processes need to update it concurrently. The reference code in this repo supports both backends via a single `SourcePriorityRegistry` class.

The schema is intentionally minimal:

```yaml
# config/source_priority.yaml
sources:
  - id: itau_macro
    priority: high
    aliases: [itaumacro, itau_economia]
    notes: |
      Brazilian sell-side macro desk. Daily morning note + monthly outlook.
      Recommended as canonical for Selic, fiscal, BoP coverage.

  - id: bcb
    priority: high
    aliases: [bancocentral, BCB_Brasil]
    notes: |
      Banco Central do Brasil official handle. Press conferences,
      Copom minutes, governor speeches.

  - id: randombr_curious
    priority: low
    notes: |
      Retail commentator. Volume is high, signal is intermittent.
      Demoted after Q1 2026 review.
```

Three required fields per entry: `id`, `priority`, and (optionally) `aliases` to handle the same source appearing under different handles in the corpus. The `notes` field is a free-text reminder for the human maintainer — it doesn't affect scoring.

A complete starter file for a Brazilian macro/markets corpus lives in [`examples/source_priority.yaml`](../examples/source_priority.yaml).

## How to populate it initially

Don't try to tag every source on day one. The pragmatic path:

1. **Run a week of queries** through the system with no priority registry (every source defaults to `normal`).
2. **Pull the top-10 sources by hit count** across that week's retrievals. These are the workhorses of your corpus.
3. **For each, decide `high` / `normal` based on whether you'd want that source's view cited in a synthesis.** Tag the obviously-noisy ones as `low`.
4. **Add new sources opportunistically.** When you notice a retrieval surfaced a source you didn't recognize, look it up and tag it. The registry grows by ~10–30 entries per month in steady state.

The system tolerates an incomplete registry. Untagged sources behave as `normal`. There is no need to tag every source before turning the layer on.

## How to maintain it over time

A monthly 15-minute review:

1. Look at the previous month's most-cited sources in evaluator output (Actor 7) — these are sources whose retrievals made it into final responses.
2. Cross-check against the priority registry. Promote a source if its track record warrants it; demote if it's been surfacing weak content.
3. Look at new sources that entered the corpus that month. Tag the ones you have a view on.

This is genuinely small work, and skipping it for a month has small consequences. The system's behaviour degrades slowly; it does not collapse.

## Pitfalls

**Tagging by perceived quality, not by demonstrated quality on your corpus.** A globally respected source can be a poor fit for a specific corpus if it covers your topics rarely or in a register the embedder doesn't align well with. Tag by "did this source's content help in retrievals I cared about" rather than by external reputation alone.

**Letting the registry calcify.** A source you tagged `high` three years ago may have rotated authors, changed coverage, or been bought out. Without monthly review, the registry drifts toward a snapshot of the past that the present queries don't match.

**Over-tagging early.** Tagging 200 sources `high` on day one because they all "seem good" gives almost no signal. The multiplier loses discriminative power when half the corpus is `high`. Aim for `high` to mean **the top 5–15% of sources**, the ones you'd genuinely want surfacing first.

**Treating the registry as a filter.** If you find yourself adding a flag `excluded: true`, you've crossed into filtering, which is a different design choice with different tradeoffs (see [`01-problem.md`](01-problem.md) on hard filters). The registry is a soft signal that multiplies score. Hard exclusions belong in a separate filter step that runs before retrieval.

## Source ID stability

The registry assumes `source_id` is stable over time. If your ingestion pipeline can change a source's ID (renormalizing Twitter handles after a rename, switching from username to user-id, etc.), the registry should be re-anchored or aliases added at the same time.

A practical rule: when ingesting, **store the original source identifier verbatim** as `raw_source_id` and a stable canonical form as `source_id`. The registry indexes on `source_id`. Renames update the canonicalization function, not 800 registry entries.

## What's next

The registry is consumed by the scoring formula in [`05-dynamic-scoring.md`](05-dynamic-scoring.md). Before that, [`04-query-understanding.md`](04-query-understanding.md) shows how the classifier produces the `source_importance` signal that controls how much the registry's votes count for a given query.
