# knowledge/

The bot answers only from the Markdown files in this folder. They are not in the public
repo: partner documentation is shared with partners under agreement, and some files hold
details the bot is told never to repeat.

Get the current set from the integration team. Files load in name order:

| File | Content |
|---|---|
| `01-b2b-api-suite.md` | B2B API Suite reference |
| `02-b2b-lead-api.md` | Lead API reference |
| `03-known-gaps.md` | Unclear or conflicting points; the bot says these need confirmation |
| `04-faq.md` | Answers from resolved partner support conversations, rewritten as general guidance (`chat_export/build_faq.py` selects the candidates) |
| `PARTNER_API_INTEGRATION_GUIDE.md` | Partner API Integration Guide (/v1) |

Rules for this folder:
- Partner-facing content only. Internal-only material (architecture notes, runbooks,
  service names) does not belong here, even with the filter in place.
- Internal names that still appear in a file are removed or rewritten at load time by the
  rules in `internal_terms.txt`; the server logs a warning at startup if any remain.
