# 01 — Why plain semantic search fails on source-named queries

Most RAG systems retrieve documents by computing the cosine similarity between the query embedding and the document embedding, then returning the top-K. This works beautifully when the user's question is content-shaped — *"how does QE affect long-end yields?"* — because the documents that talk about QE and long-end yields share vector neighborhoods with that query.

It fails, sometimes silently and sometimes catastrophically, on three common patterns.

## Pattern 1 — Source-named queries

> *"What does Goldman think about EM allocation in 2026?"*

The user is **not** asking "what's the consensus on EM allocation." They're asking what one specific source's view is. Plain semantic search retrieves whatever document is most semantically aligned with the *content* of the query — which is often a sell-side note from a competitor that quotes Goldman in passing, or a news article summarizing Goldman's view second-hand, or worst of all, a high-similarity tweet from a retail account using the same vocabulary.

The Goldman note itself, if it's in the corpus, often loses on pure similarity because:

- Research notes are long, with extensive boilerplate, charts, and disclaimers diluting the semantic signal.
- The Goldman analyst writing the note uses house-style hedged language ("we believe", "we expect", "in our view") rather than mirroring the user's phrasing.
- The note may not contain the exact phrase "EM allocation" — it may say "emerging-market equity overweight" or "GEM positioning."

The user gets back five tweets summarizing what Goldman said, instead of the actual Goldman view.

## Pattern 2 — Institutional / entity queries

> *"What is the BCB saying about Selic this week?"*

Same family as Pattern 1, but stricter. The user wants:

- Documents authored by the BCB (press conferences, monetary policy committee minutes, governor speeches)
- Optionally, documents discussing the BCB's view, but ranked **below** the BCB's own statements

Plain semantic search has no awareness of authorship. A reasonable similarity threshold retrieves a mix of BCB documents and discussion-of-BCB documents in roughly random order. There is no mechanism to say "show me the BCB's voice first."

## Pattern 3 — Quality-aware queries that don't name a source

> *"What's the fiscal outlook for the second half?"*

The user is not naming a source. But they implicitly want **good** sources, not random ones. If the corpus contains:

- Three rigorous notes from a sell-side fiscal strategist
- Two well-sourced columns from a respected economics columnist  
- Forty tweets from various retail commentators
- One mainstream-media headline-summary article

Plain semantic search returns whichever cluster wins on cosine. If the retail tweets happen to use the user's exact phrasing ("fiscal outlook second half"), they win — and the rigorous note from the strategist loses on text-match grounds even though it's by far the more authoritative answer.

## What goes wrong specifically

The shared failure across all three patterns is that **the embedding model does not know what your sources are**. To the embedder, a tweet by `@randomBRcurious` and a research desk note from Itaú Macro are points in a 1,024-dimensional space, judged solely by surface text. A retrieval system that scores only on that space inherits the embedder's blindness.

Adding a hard filter — "only retrieve from this list of high-priority sources" — solves the failure but creates worse ones:

- Most queries don't name a source. Applying the filter universally throws away signal.
- Even when the user names a source, the *right answer* may not be from that source. If the user asks about Goldman and Goldman hasn't written about the topic, the system must gracefully fall back.
- The list of "high-priority sources" is corpus-dependent, time-dependent, and topic-dependent. A static allow-list ages badly.

## What this system does instead

The next four docs build the solution layer by layer:

1. A **priority registry** ([`03-priority-registry.md`](03-priority-registry.md)) tags every source with a quality level, but doesn't filter on it.
2. A **classifier** ([`04-query-understanding.md`](04-query-understanding.md)) reads each query and outputs a single number, `source_importance` (0.0 – 0.8), saying how much source identity matters *for this query*.
3. A **dynamic scoring formula** ([`05-dynamic-scoring.md`](05-dynamic-scoring.md)) blends semantic similarity, source priority, and freshness with weights driven by `source_importance`.
4. **Boost lanes** ([`06-entity-boost.md`](06-entity-boost.md), [`07-institution-boost.md`](07-institution-boost.md), [`08-keyword-boost.md`](08-keyword-boost.md)) pull extra rows when the query explicitly names something, ensuring the named source is retrieved even when semantic similarity ranks it low.

No layer hard-filters. Every document in the corpus remains reachable. The layers reshape the ranking so that source identity contributes to score proportional to how much the user actually cared about source identity in their query.

Move on to [`02-architecture.md`](02-architecture.md) for the integrated picture.
