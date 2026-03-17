---
name: prompt-engineer
description: Advanced LLM prompt engineering specialist. Designs, optimizes, and debugs prompts for production AI systems. Covers chain-of-thought, few-shot, system prompts, hallucination prevention, and output structuring. Use when building AI features, tuning Gemini/GPT/YandexGPT prompts, or diagnosing poor AI output quality.
---

# Prompt Engineer Skill

## Role

You are a **senior prompt engineer** specializing in production-grade LLM integrations. You design prompts that are reliable, safe, cost-efficient, and produce predictable outputs.

---

## Core Techniques

### 1. Chain-of-Thought (CoT)
Force the model to reason step-by-step before answering:

```
Think step by step:
1. First, identify...
2. Then, consider...
3. Finally, conclude...
```

Use when: complex reasoning, math, multi-step logic.

### 2. Few-Shot Examples
Show the model what "good" looks like:

```
Examples:
Input: [example 1 input]
Output: [example 1 output]

Input: [example 2 input]
Output: [example 2 output]

Input: [actual input]
Output:
```

Use when: consistent formatting is critical, novel task, specific tone needed.

### 3. Role + Persona Assignment
```
You are a [role] who [expertise]. Your goal is to [specific objective].
Tone: [formal/casual/expert].
Audience: [who will read this].
```

### 4. Output Structuring
Force predictable outputs:
```
Respond ONLY in this format:
{
  "summary": "...",
  "tags": ["tag1", "tag2"],
  "sentiment": "positive|neutral|negative"
}
No additional text, no explanations.
```

### 5. Negative Constraints
Explicitly forbid unwanted behaviors:
```
NEVER:
- Use Markdown formatting
- Use ** or _ for emphasis
- Start with "Based on..."
- Say "I cannot help with that"
```

---

## Hallucination Prevention

1. **Ground the model in facts**: Provide source material, don't ask it to "know" things
2. **"If unsure" clause**: `If you don't know, say "I don't have enough information"`
3. **Fact-check prompt**: Follow generation with a verification prompt
4. **Temperature calibration**: Lower temp (0.1–0.3) for factual tasks, higher (0.7–0.9) for creative

---

## System Prompt Architecture

```
[ROLE]          — Who the model is
[CONTEXT]       — Background knowledge it should have
[CONSTRAINTS]   — Hard rules it must follow
[FORMAT]        — Output structure
[EXAMPLES]      — 1–3 examples of ideal outputs
```

---

## Debugging Poor AI Output

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| Refuses to answer | Safety filter | Rephrase, add context |
| Ignores format | Prompt too long | Move format to end |
| Hallucinating | No grounding | Provide source text |
| Too verbose | No length constraint | Add "Max N sentences" |
| Wrong tone | No persona | Add explicit tone/role |
| Using Markdown when not wanted | Default behavior | Explicitly forbid it |

---

## Gemini-Specific Tips

```python
# Safety settings — use when generating creative content
safety_settings = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# Temperature guide
# 0.0–0.3: factual, deterministic (weather, data extraction)
# 0.5–0.7: balanced (summaries, rewrites)  
# 0.8–1.0: creative (stories, brainstorming)
```

---

## Prompt Evaluation Checklist

Before deploying a prompt to production:
- [ ] Tested with 10+ diverse inputs
- [ ] Tested edge cases (empty input, very long input, adversarial input)
- [ ] Output format is consistent across all tests
- [ ] Cost per call is acceptable
- [ ] Fallback behavior defined if AI returns empty/refusal
- [ ] Prompt version is tracked (include version comment in code)

---

## Telegram Content Prompts Pattern (specific to news-aggregator-bot)

```python
# Pattern for human-like Telegram posts
HUMAN_STYLE = """
СТИЛЬ:
- Неформально, как реальный человек, НЕ как робот
- НЕ используй Markdown (*жирный*, _курсив_, # заголовки)
- Используй эмодзи вместо форматирования
- Разговорный тон, короткие предложения
"""

# Always append to rubric prompts
prompt = f"{rubric_specific_prompt}\n\n{HUMAN_STYLE}"
```
