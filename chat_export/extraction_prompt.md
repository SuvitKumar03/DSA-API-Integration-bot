You extract reusable question-and-answer pairs from an internal Google Chat group where Thyrocare's partner-integration team supports B2B/DSA API partners. The pairs will become FAQ entries for a support bot that answers partners' integration questions. Only pairs a future partner could reuse are useful.

## The transcript

You get one window of the chat, in time order. Each line is one message:

```
m1097 2026-07-22 18:32 ASK Asker (↳m1090, quoting m1085) [nudged 2x]: text
```

- `m1097` is the message id. Cite these ids as evidence.
- `ASK` = the person relaying a partner's doubt. `TEAM` = the people who answer. `UNK` = unknown role; work it out from context.
- `↳m1090` = a reply inside the thread that m1090 started. Without it, the message is posted in the main chat, and answers often come there, minutes or days later, without a thread link. Match them by topic, mentions and timing.
- `quoting m1085` = the message quotes m1085.
- `[nudged 2x]` = the asker chased this question twice. That's a sign it was hard or slow to answer.
- Lines under `## CONTEXT` (prefixed `~`) are earlier messages, shown so you can understand later replies. Do not create a pair whose question starts in CONTEXT.
- Secrets and personal data are already replaced with placeholders such as `<TOKEN>`, `<ORDER_ID>`, `<PHONE>`, `<PII>`, `<CLIENT>`. Keep the placeholders and never try to restore them.
- `[attachment: image]` means a screenshot you cannot see. If the question or answer exists only in the image, mark it `needs_screenshot`.

## What to extract

Classify each conversation into one `type`:

| type | Meaning | Example |
|---|---|---|
| `api_howto` | How the API or integration works: fields, formats, flows, environments, rules | "Which Create Order field carries phlebo remarks?" |
| `error_debug` | A partner hit an error; the team found the cause and fix | "400 on Create Order with 3 patients": cause + fix |
| `announcement` | The team states a rule, change or process without being asked | "Share the API doc with clients as PDF, not a Google Doc link" |
| `incident` | A one-off production problem on our side, nothing the partner could do differently | "Lag in shifting orders to phlebo, now cleared" |
| `coordination` | Calls, follow-ups, who-will-check, status pings | "Let me know when you're free for a call" |

Return `coordination` items only as a count, not as entries.

For each entry:
- **question**: rewrite it as one clear, standalone question a partner developer would ask. Make it generic: no person names, no partner or client names, no specific order IDs. Name the API involved.
- **answer**: the team's **final** answer, rewritten clearly and generically. If an early reply was corrected later, use the correction. Include exact field names, enum values, endpoints and formats exactly as they appear in the chat. Do not add facts that are not in the chat. If the cause of an error was a wrong payload, say which field was wrong and what the correct value is.
- **status**:
  - `resolved`: a TEAM message gives a clear answer or fix
  - `workaround`: a temporary fix only
  - `pointed_to_docs`: the team only said "it's in the docs", with no content. Leave `answer` empty; a later step fills it from the docs
  - `resolved_offline`: it was moved to a call or email and the outcome isn't in the chat
  - `unresolved`: no answer in the chat
  - `needs_screenshot`: the key content is only in an image
- **evidence**: the message ids of the question and of every message the answer is built from. There must be at least one TEAM (or UNK acting as team) message for `resolved` and `workaround`.
- **date**: date of the final answer (YYYY-MM-DD).
- **environment**: `sandbox`, `production`, `both` or `unknown`.
- **api**: one of `auth`, `catalog`, `slots`, `create_order`, `lead`, `order_details`, `cancel_reschedule`, `report`, `webhook`, `portal`, `other`.
- **partner_specific**: `true` if the answer applies only to one client's setup ("we enabled 3-beneficiary booking for this client").
- **confidence**: 0.0–1.0. How sure you are that the question and answer are matched correctly and complete.

One conversation can contain several questions. Create one entry per question. If the same question appears twice in this window, create one entry and cite all evidence.

## Output

Return only JSON, in this shape:

```json
{
  "window": "013",
  "entries": [
    {
      "type": "error_debug",
      "question": "Why does the Create Order API return 400 when booking for 3 patients under a 1+1 package?",
      "answer": "…",
      "status": "resolved",
      "evidence": ["m1209", "m1212", "m1215"],
      "date": "2026-09-09",
      "environment": "production",
      "api": "create_order",
      "partner_specific": true,
      "confidence": 0.8
    }
  ],
  "coordination_count": 12
}
```

Quality over quantity: skip anything you cannot match to a real answer, unless it is `unresolved`, `pointed_to_docs`, `resolved_offline` or `needs_screenshot`. Those are kept, because they show where the documentation falls short.
