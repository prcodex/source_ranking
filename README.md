# Source Ranking & Boost — a retrieval-scoring extension for `m3xabr-core`

> When a user asks *"what does Goldman think about inflation?"*, plain semantic search returns the document that talks most about inflation. The user wanted the document **from Goldman**. This repo describes the layered scoring system that closes that gap.

This is a description-plus-reference-implementation of the source-ranking and boost stack that runs in **production M3xA**, written so it drops into the public [`m3xabr-core`](https://github.com/prcodex/M3XABR_NEW) 7-actor architecture with minimal surface change.

## What's interesting about it

Most RAG agents use one of two scoring approaches:

1. **Pure semantic similarity** — fast, but blind to "who said it." Treats a tweet from a random account and a research note from the head of strategy as interchangeable if their text vectors are close.
2. **Hard source filters** — boolean "only include Goldman" toggle. Works when the user names a source explicitly, useless when they don't, and brittle when the relevant source isn't obvious.

This system is a **dynamic blend** of the two. A cheap Haiku call classifies each query and outputs a single number — `source_importance` (0.0 – 0.8) — that controls how much source identity matters for *this specific query*. A vanilla "what's happening in fiscal policy?" gets `si = 0.0` (pure semantic), while "what does the BCB say about Selic?" gets `si = 0.6` (source dominates). The retrieval pipeline reads `si` and dynamically rebalances four scoring lanes — semantic, source priority, freshness, and explicit-entity boost — without any source ever being hard-filtered out.

## When to use this

- Your corpus has **named, opinion-bearing sources** (research desks, institutions, individual analysts, central banks) where the author identity carries information.
- Some users name sources explicitly (*"what does Itaú say"*), some don't (*"fiscal outlook"*), and the same retrieval stack has to serve both.
- You already have a **classifier actor** (m3xabr-core: `actors/classifier.py`) producing structured query metadata via Haiku — extending it is cheaper than adding a new actor.
- You're willing to maintain a **priority registry** (yaml or sqlite) tagging your sources as `high` / `normal` / `low`. The registry is the only manual labour; everything else is automatic.

## The four layers

| # | Layer | What it does | Where it lives |
|---|---|---|---|
| 1 | **Priority registry** | Maps every source ID to `high` / `normal` / `low` | `config/source_priority.yaml` |
| 2 | **Query understanding** | Haiku reads the query, outputs `source_importance` (0.0–0.8) and `boost_keywords` | extension to `actors/classifier.py` |
| 3 | **Dynamic scoring** | Blends semantic / source / freshness with weights driven by `si` | `m3xabr_core/scoring.py` (new) |
| 4 | **Boost lanes** (Entity + Institution + Keyword) | Pulls *additional* rows when the query names specific entities / institutions, and additively boosts on keyword hits | `actors/booster.py` (new) |

Read them in order:

1. [`docs/01-problem.md`](docs/01-problem.md) — why plain semantic fails
2. [`docs/02-architecture.md`](docs/02-architecture.md) — the 4-stage pipeline, with mermaid showing how it slots into m3xabr-core's existing 7 actors
3. [`docs/03-priority-registry.md`](docs/03-priority-registry.md) — building and maintaining the `source_priority.yaml`
4. [`docs/04-query-understanding.md`](docs/04-query-understanding.md) — extending `classifier.py` to emit `source_importance` + `boost_keywords`
5. [`docs/05-dynamic-scoring.md`](docs/05-dynamic-scoring.md) — the formula, with worked numerical examples
6. [`docs/06-entity-boost.md`](docs/06-entity-boost.md) — extra-row pulls when a single entity is named
7. [`docs/07-institution-boost.md`](docs/07-institution-boost.md) — FROM-the-source vs MENTIONS-the-source two-lane pattern
8. [`docs/08-keyword-boost.md`](docs/08-keyword-boost.md) — additive boost + threshold relaxation
9. [`docs/09-integration.md`](docs/09-integration.md) — wiring it all into the m3xabr-core pipeline

Reference code (drops into `m3xabr_core/` paths):

- [`reference/scoring.py`](reference/scoring.py) — the dynamic blend formula
- [`reference/booster.py`](reference/booster.py) — entity + institution + keyword lanes
- [`reference/classifier_addendum.py`](reference/classifier_addendum.py) — schema + prompt extensions to `actors/classifier.py`

Examples:

- [`examples/source_priority.yaml`](examples/source_priority.yaml) — a starter Brazilian-corpus priority table
- [`examples/worked_query_trace.md`](examples/worked_query_trace.md) — step-by-step trace of three real queries through the full stack

## Numbers and constants

The constants quoted throughout the docs — the `1.5×` high-priority multiplier, the `0.55` slope on `si`, the `0.20` minimum-similarity threshold for boosted rows — are the **values that converged in M3xA production after ~1 quarter of tuning on a macro/Brazil corpus**. They are not load-bearing for the architecture; they are starting points. The `docs/09-integration.md` file describes the dial-by-dial tuning procedure you'd run on your own corpus to land on the right numbers for your distribution of queries.

## What this doesn't do

- Does **not** rerank with a cross-encoder or a second LLM call. The scoring is purely vector-similarity + source-priority + freshness + boost-keyword arithmetic. A cross-encoder is a separate, slower decision that can be layered after this; the boost stack runs cheaply enough to live in every query path.
- Does **not** handle source deduplication, fact-checking, or contradiction resolution between sources. Source ranking decides *who gets retrieved*, not *who's right*.
- Does **not** require a graph database, knowledge graph, or named-entity-recognition pipeline. The entity registry is a flat YAML.

## License

MIT (see `LICENSE`).
