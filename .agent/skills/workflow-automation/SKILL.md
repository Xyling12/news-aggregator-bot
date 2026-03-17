---
name: workflow-automation
description: Architect reliable, observable, and scalable automated workflows. Covers sequential, parallel, and orchestrator-worker patterns, idempotency, retry strategies, and monitoring. Use when building background jobs, content schedulers, bot automation, or multi-step async pipelines in Python/Node.js.
---

# Workflow Automation Skill

## Role

You are a **workflow systems architect**. You design automated pipelines that are reliable under failure, observable in production, and easy to maintain and extend. You don't just make things run — you make them run **reliably** at 3am when no one is watching.

---

## Core Patterns

### 1. Sequential Workflow
```python
async def run_sequential():
    result_a = await step_a()
    result_b = await step_b(result_a)
    result_c = await step_c(result_b)
    return result_c
```
Use when: order matters, each step depends on the previous.

### 2. Parallel Workflow
```python
async def run_parallel():
    results = await asyncio.gather(
        step_a(),
        step_b(),
        step_c(),
        return_exceptions=True  # Don't let one failure kill all
    )
    # Handle partial failures
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error(f"Step {i} failed: {r}")
```
Use when: steps are independent and can run simultaneously.

### 3. Orchestrator-Worker Pattern
```python
# Orchestrator
async def orchestrate(items: list):
    tasks = [worker(item) for item in items]
    return await asyncio.gather(*tasks, return_exceptions=True)

# Worker (stateless, idempotent)
async def worker(item):
    try:
        result = await process(item)
        await save(result)
        return result
    except Exception as e:
        # Worker fails gracefully — orchestrator handles it
        raise WorkerError(item.id, str(e)) from e
```

---

## Reliability Patterns

### Idempotency (the #1 rule)
Every workflow step must be safe to run twice:
```python
# Bad: creates duplicate on retry
await db.insert(item)

# Good: upsert — safe to call multiple times
await db.upsert(item, on_conflict="update")

# For scheduled jobs: slot key prevents double-publish
slot_key = f"{today}_{rubric}"
if slot_key in published_today:
    return  # Already done
```

### Retry with Exponential Backoff
```python
async def with_retry(fn, max_retries=3, base_delay=1.0):
    for attempt in range(max_retries):
        try:
            return await fn()
        except TransientError as e:
            if attempt == max_retries - 1:
                raise
            wait = base_delay * (2 ** attempt)
            logger.warning(f"Retry {attempt + 1}/{max_retries} after {wait}s: {e}")
            await asyncio.sleep(wait)
```

### Circuit Breaker
```python
class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_time=60):
        self.failures = 0
        self.threshold = failure_threshold
        self.recovery_time = recovery_time
        self.last_failure = None
        self.state = "closed"  # closed=normal, open=failing, half-open=testing
    
    async def call(self, fn):
        if self.state == "open":
            if time.time() - self.last_failure > self.recovery_time:
                self.state = "half-open"
            else:
                raise CircuitOpenError("Service unavailable")
        try:
            result = await fn()
            self.failures = 0
            self.state = "closed"
            return result
        except Exception as e:
            self.failures += 1
            self.last_failure = time.time()
            if self.failures >= self.threshold:
                self.state = "open"
                logger.error(f"Circuit breaker OPEN: {e}")
            raise
```

---

## Scheduler Patterns (Python asyncio)

### Time-Based Scheduler
```python
async def scheduler_loop():
    while running:
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        
        for hour, minute, job_name in SCHEDULE:
            slot_key = f"{today}_{job_name}"
            
            # 2-minute publish window
            if now.hour == hour and minute <= now.minute < minute + 2:
                if slot_key not in done:
                    await run_job(job_name)
                    done.add(slot_key)
            
            # 30-minute catch-up window (for restarts)
            elif now.hour == hour and minute <= now.minute < minute + 30:
                if slot_key not in done:
                    logger.info(f"Catch-up: {job_name}")
                    await run_job(job_name)
                    done.add(slot_key)
        
        await asyncio.sleep(30)
```

---

## Observability

Every workflow should emit:

```python
@dataclass
class WorkflowEvent:
    workflow_id: str
    step: str
    status: Literal["started", "completed", "failed", "skipped"]
    duration_ms: int
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)
```

### Structured Logging Pattern
```python
logger.info(
    "step_completed",
    extra={
        "workflow": "content_scheduler",
        "rubric": "weather",
        "duration_ms": elapsed,
        "photo_found": bool(photo_url),
        "chars": len(text),
    }
)
```

---

## Anti-Patterns

| Anti-Pattern | Problem | Fix |
|-------------|---------|-----|
| Monolithic workflow | One failure = all fails | Break into independent steps |
| No error handling | Silent failures | Always handle exceptions explicitly |
| Non-idempotent steps | Duplicate data on retry | Use upserts, slot keys, dedup hashes |
| No timeout | Hangs forever | Always set `timeout=aiohttp.ClientTimeout(total=N)` |
| Catching all exceptions | Hides bugs | Catch specific exception types |
| No admin notification | Failures go unnoticed | Notify on critical failures |

---

## Deployment Checklist

- [ ] All steps are idempotent
- [ ] Retry logic for transient failures
- [ ] Timeout on all external calls  
- [ ] Structured logging with step names
- [ ] Admin notifications on critical failures
- [ ] Graceful shutdown handler (SIGTERM → drain queue → exit)
- [ ] Health check endpoint (returns `{"status": "ok", "last_run": ...}`)
- [ ] Catch-up logic for restarts
