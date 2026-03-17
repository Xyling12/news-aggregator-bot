---
name: content-orchestrator
description: Automates content creation pipelines — from source ingestion to publication. Covers scheduling, multi-platform distribution, quality gates, and fallback strategies. Use when building content bots, news aggregators, auto-publishing systems, or scheduled content pipelines.
---

# Content Orchestrator Skill

## Role

You are a **content pipeline architect**. You design systems that ingest raw data (news, APIs, user input), transform it into engaging content, apply quality gates, and publish it reliably across channels.

---

## Pipeline Architecture

```
[Sources]  →  [Ingestor]  →  [Ranker/Filter]  →  [Generator]  →  [Quality Gate]  →  [Publisher]  →  [Analytics]
```

### Source Types
- **RSS/Atom feeds**: Parse with feedparser, normalize to unified schema
- **APIs**: Rate-limit aware clients with retry/backoff
- **Scrapers**: Respectful crawlers with caching (avoid hammering sites)
- **Manual**: Admin-submitted content via bot commands or CMS

### Ingestor Responsibilities
- Deduplication (hash of title+URL, or semantic similarity)
- Normalization (unified `Article` schema: id, title, body, url, published_at, source)
- Source credibility scoring (whitelist/blacklist of domains)

### Ranker/Filter
- Recency score (decay function: newer = higher weight)
- Relevance score (keyword match, category classification)
- Engagement potential (predicted based on past published items)
- **Dedup threshold**: cosine similarity > 0.85 → skip as duplicate

---

## Scheduling Patterns

### Fixed-time Schedule (current news-aggregator-bot pattern)
```python
SCHEDULE = [
    (7, 0, "weather"),
    (9, 0, "history"),
    (11, 0, "facts"),
    ...
]
# Pros: predictable, user-friendly timing
# Cons: inflexible, may miss breaking news
```

### Event-Driven (for breaking news)
```python
# Trigger immediately when high-priority item arrives
async def on_breaking_news(article):
    if article.score > BREAKING_THRESHOLD:
        await publish_immediately(article)
```

### Hybrid (recommended)
- Fixed rubrics at fixed times (weather, history, recipes)
- Breaking news queue with immediate publish + dedup window (30 min)

---

## Quality Gates

Before publishing, content must pass ALL gates:

```python
def quality_check(text: str) -> tuple[bool, str]:
    if len(text) < 100:
        return False, "Too short"
    if is_refusal(text):  # AI safety refusal
        return False, "AI refusal"
    if contains_hallucination_markers(text):
        return False, "Possible hallucination"
    if markdown_ratio(text) > 0.1:  # Too much raw Markdown
        return False, "Formatting issue"
    return True, "OK"
```

### Refusal Detection
```python
REFUSAL_PATTERNS = [
    "я не могу", "не могу обсудить", "это вне моих",
    "i cannot", "i'm unable to", "i can't help",
    "as an ai", "как ии"
]

def is_refusal(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in REFUSAL_PATTERNS)
```

---

## Multi-Platform Distribution

```python
async def publish(content: Content):
    tasks = []
    
    # Telegram (primary)
    tasks.append(publish_telegram(content))
    
    # VK (secondary, if enabled)
    if config.vk_enabled:
        tasks.append(publish_vk(content))
    
    # Archive to DB
    tasks.append(save_to_db(content))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Handle partial failures gracefully
```

---

## Fallback Strategies

| Failure | Fallback |
|---------|----------|
| AI returns empty | Retry once with lower temperature |
| AI refusal | Skip rubric, log, notify admin |
| Photo not found | Publish text-only |
| Photo URL fails | Download locally → BufferedInputFile |
| All weather APIs fail | Skip weather post (never invent data!) |
| Telegram rate limit | Queue + exponential backoff |

---

## Content Performance Tracking

Track for each published item:
- `views`, `forwards`, `reactions`, `comments`
- Feed back into ranker to improve future selections

```python
# Monthly performance report
SELECT rubric, AVG(reactions), COUNT(*) 
FROM published_content 
GROUP BY rubric 
ORDER BY AVG(reactions) DESC
```

---

## Best Practices

1. **Never invent factual data** (weather, statistics, dates) — always use real APIs
2. **Idempotency**: Use `slot_key = f"{date}_{rubric}"` to prevent double-publishing
3. **Catch-up window**: If bot was down, publish late posts within 30-minute window
4. **Admin notifications**: Always notify admins of auto-published content
5. **Version prompts**: Track prompt versions in code comments for A/B testing
6. **Topic rotation**: Use `_used_topics` dict to avoid repeating the same topic
