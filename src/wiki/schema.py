"""
Wiki Schema — conventions that guide the LLM as wiki maintainer.

This file defines the structure, page types, and workflows for the
cycling agent's LLM Wiki. It is read by the LLM during ingest and
query operations to ensure consistent wiki maintenance.

Domain focus areas:
- Performance evaluation (training metrics, power analysis, load management)
- Wellness data (HRV, sleep, recovery, readiness)
- Personal ML (models on user data, feature engineering, predictions)
- Health research (knee issues, recovery protocols, non-cycling exercises)
"""

WIKI_SCHEMA = """\
# Cycling Agent Wiki Schema

## Purpose

This wiki is a personal knowledge base for cycling performance, wellness,
machine learning on personal data, and health research. It focuses on four
domains:

1. **Performance** — training metrics, power analysis, load management, FTP, TSS
2. **Wellness** — HRV, sleep, recovery, readiness scores, daily wellness
3. **ML** — models trained on personal data, feature engineering, predictions
4. **Health** — knee issues, recovery protocols, non-cycling exercises, injury prevention

## Directory Structure

```
wiki/
  index.md          # Content catalog — updated on every ingest
  log.md            # Chronological activity log (append-only)
  entities/         # Entity pages (equipment, conditions, people, places)
  concepts/         # Concept pages (training, physiology, ML, health)
  sources/          # Source summaries (one per ingested document)
  analyses/         # LLM-generated analyses and comparisons
  syntheses/        # Cross-topic synthesis pages
```

## Page Types

### Entity Pages (`entities/`)

```markdown
---
type: entity
category: <equipment|person|place|organization|health_condition|other>
created: <ISO date>
updated: <ISO date>
sources: [<source-ref>, ...]
---

# <Entity Name>

## Summary
One-paragraph overview of what this entity is and why it matters.

## Key Facts
- Fact 1
- Fact 2

## Related Concepts
- [[Concept Name]] — brief connection

## Related Entities
- [[Entity Name]] — brief connection

## Source History
| Source | Date | Key Information |
|--------|------|----------------|
| [[Source: Title]] | YYYY-MM-DD | What this source contributed |
```

### Concept Pages (`concepts/`)

```markdown
---
type: concept
category: <performance|wellness|ml|health|nutrition|recovery|other>
created: <ISO date>
updated: <ISO date>
sources: [<source-ref>, ...]
confidence: <high|medium|low>
---

# <Concept Name>

## Definition
Clear, concise definition.

## Key Points
- Point 1 with evidence
- Point 2 with evidence

## Application
Specific application to cycling training, performance, or health.

## Evidence Quality
Summary of evidence strength across sources.

## Related Concepts
- [[Concept Name]] — brief connection

## Related Entities
- [[Entity Name]] — brief connection

## Source History
| Source | Date | Key Information |
|--------|------|----------------|
| [[Source: Title]] | YYYY-MM-DD | What this source contributed |
```

### Source Summaries (`sources/`)

```markdown
---
type: source
title: <Original Title>
author: <Author if known>
date: <Publication or ingest date>
url: <URL if applicable>
format: <article|paper|book|video|podcast|data|other>
domain: <performance|wellness|ml|health>
ingested: <ISO datetime>
---

# Source: <Title>

## Summary
2-3 paragraph summary of the source's main content.

## Key Takeaways
- Takeaway 1
- Takeaway 2

## Entities Mentioned
- [[Entity Name]] — role in this source

## Concepts Discussed
- [[Concept Name]] — how discussed

## Quotes
Notable direct quotes with context.

## Notes
Any additional context, contradictions with other sources, or flags.
```

### Analysis Pages (`analyses/`)

```markdown
---
type: analysis
question: <Original question or analysis topic>
domain: <performance|wellness|ml|health>
created: <ISO date>
sources: [<source-ref>, ...]
---

# Analysis: <Topic>

## Question
What was being investigated.

## Answer
Synthesized answer with citations.

## Supporting Evidence
Evidence from wiki pages.

## Related Pages
- [[Page Name]]
```

## Cross-Reference Convention

All wiki pages use `[[Page Name]]` syntax for internal links. The LLM
must maintain these links bidirectionally — when Page A links to Page B,
Page B should also link back to Page A in its "Related" section.

## Ingest Workflow

When a new source is ingested:
1. Read the source content
2. Create a source summary page in `sources/`
3. Identify entities and concepts mentioned
4. Create or update entity pages in `entities/`
5. Create or update concept pages in `concepts/`
6. Note any contradictions with existing pages
7. Update `index.md` with all changes
8. Append an entry to `log.md`

## Query Workflow

When answering a question:
1. Read `index.md` to find relevant pages
2. Read the identified pages
3. Synthesize an answer with citations
4. If the answer is substantial, offer to file it as an analysis page

## Lint Workflow

Periodically check for:
- Contradictions between pages
- Stale claims superseded by newer sources
- Orphan pages with no inbound links
- Important concepts mentioned but lacking their own page
- Missing cross-references
"""


def schema_text() -> str:
    """Return the wiki schema text for LLM prompts."""
    return WIKI_SCHEMA