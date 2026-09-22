# Jev (TypeSafe AI) — via OpenRouter

**What it is:** Jev is TypeSafe AI's "System One" model. Unlike a standard LLM, it doesn't generate free-form text — it returns **structured decisions** against a fixed schema. You give it content (a string, JSON object, or array) plus one or more typed questions, and it returns classifications/decisions with confidence, not prose.

## Decision types

| Type   | Example                                                              | Output                                  |
|--------|-----------------------------------------------------------------------|------------------------------------------|
| Choice | "Which team should handle this ticket?" (billing / technical / sales) | One label, with probabilities for each   |
| Score  | "How urgent is this?" (can wait / this week / blocking revenue)       | Ranked position on an ordered scale      |
| Noul   | "Is this a bug report?" (true/false with defined criteria per side)   | Binary decision                          |

## Typical uses

- Routing support tickets to the right team
- Verifying whether retrieved context actually supports an LLM-generated draft answer (cheap-model verification before escalating to a frontier model)
- Fact-checking a claim against retrieved evidence
- Lightweight content classification/moderation

A common cost-saving pattern: a cheap model drafts an answer from retrieved context, Jev checks if the context actually supports that draft, and a frontier model only rewrites the answer if Jev's check fails. Most requests never reach the expensive model.

## Access

- Available through **OpenRouter** at a dedicated endpoint: `https://openrouter.ai/api/v1/systemone`
- Requires an OpenRouter API key **with access to Jev specifically** (it's a separate grant, not automatic with a general OpenRouter key)
- Model IDs: bare `jev-1.13` → auto-mapped to `typesafe/jev-1.13`; `jev-latest` → mapped to `~typesafe/jev-latest` (always the newest release)

## Python example (raw HTTP)

```python
import os, requests

resp = requests.post(
    "https://openrouter.ai/api/v1/systemone",
    headers={
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
    },
    json={
        "model": "typesafe/jev-1.13",  # or "~typesafe/jev-latest"
        "state": {
            "customer_tier": "enterprise",
            "ticket": "My checkout page shows a blank screen after I click Pay.",
        },
        "questions": {
            "is_bug": {
                "type": "noul",
                "instructions": "Is the customer reporting a software defect?",
                "criteria": {
                    "true": "Describes broken or unexpected product behavior.",
                    "false": "Asking a question or requesting a feature.",
                },
            },
            "team": {
                "type": "choice",
                "instructions": "Which team should own this ticket?",
                "criteria": {
                    "account": "Login, permissions, or profile issues.",
                    "payments": "Checkout, billing, or payment processing issues.",
                },
            },
        },
    },
)
print(resp.json())
```

## TypeSafe SDK (typed clients)

If you're already using TypeSafe directly, point its own SDK at OpenRouter instead of switching to raw HTTP or OpenRouter's generic SDKs — same request/response shapes, no need to hand-build the JSON body above.

- Packages: `@typesafe-ai/sdk` (npm) / `typesafe_sdk` (PyPI)
- Auth: pass `apiKey`/`api_key` (your OpenRouter key), or set the `TYPESAFE_API_KEY` env var
- Base URL: `https://openrouter.ai/api` — the SDK appends `/v1/systemone` itself, so don't include that suffix. Settable via constructor `baseURL`/`base_url` or the `TYPESAFE_BASE_URL` env var.
- Model IDs: same bare-ID mapping as raw HTTP (`jev-1.13` → `typesafe/jev-1.13`)

**Python:**
```python
import os
from typesafe_sdk import TypeSafeClient

client = TypeSafeClient(
    api_key=os.environ["OPENROUTER_API_KEY"],
    base_url="https://openrouter.ai/api",
)

result = client.system_one(
    model="jev-1.13",
    state="I was charged twice for my subscription.",
    questions={
        "refund": {"type": "noul", "instructions": "Is the customer asking for money back?"},
        "department": {
            "type": "choice",
            "instructions": "Which team should handle this?",
            "criteria": {"billing": "Charges and refunds", "technical": "Bugs and outages"},
        },
    },
)
```

**Gotcha:** the SDK's model-listing call (`client.models.list()` → `GET /api/v1/models`) doesn't work through OpenRouter — it expects a TypeSafe-shaped response and rejects OpenRouter's, so stick to `systemOne`/`system_one` calls only.

## Output schema

The response has one entry in `answers` per question key, shaped by that question's `type`:

```
{
  "model": string,
  "answers": {
    "<question_key>": (
      // type: "noul"
      { "type": "noul", "noul": number }               // 0–1

      // type: "choice"
      | { "type": "choice", "choice": string,           // one of the criteria labels
          "probabilities": { "<criteria_label>": number, ... },
          "confidence": number }                        // 0–1

      // type: "score"
      | { "type": "score", "score": number,             // 0–1, position on the scale
          "legend": { "<index>": { "label": string, "description": string }, ... },
          "probabilities": { "<index>": number, ... },
          "confidence": number }                        // 0–1
    ), ...
  },
  "usage": {
    "input_tokens": number,
    "output_tokens": number,
    "cost": number                                      // USD
  },
  "id": string,
  "provider": string
}
```

`noul` answers come back bare (no `probabilities`/`confidence`); `choice` and `score` include both.

## Notes

- OpenRouter also has official SDKs (Python, TypeScript, Go) that wrap the System One endpoint more ergonomically — `openrouter` on PyPI / `@openrouter/sdk` on npm.
- Docs: `https://openrouter.ai/docs/guides/community/typesafe-sdk`