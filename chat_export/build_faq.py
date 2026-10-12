"""Select publishable FAQ candidates from the extracted Q&A entries.

Keeps only resolved, generic (not partner-specific), confident how-to / error entries,
drops incidents and announcements (they describe one-off outages, not the API contract),
and runs every candidate through the bot's internal-name filter. The output is a review
file; a person rewrites and dedupes it into knowledge/04-faq.md.

Run: python chat_export/build_faq.py --space AAAAxNg3VYI [--min-confidence 0.7]
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from bot.guardrails import redact, scrub_secrets  # noqa: E402

KEEP_TYPES = {"api_howto", "error_debug", "policy"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", required=True)
    ap.add_argument("--min-confidence", type=float, default=0.7)
    args = ap.parse_args()

    base = HERE / "output" / args.space / "extracted"
    entries = json.loads((base / "all_entries.json").read_text(encoding="utf-8"))

    kept, skipped = [], defaultdict(int)
    for e in entries:
        if e.get("type") not in KEEP_TYPES:
            skipped[f"type={e.get('type')}"] += 1
        elif e.get("status") != "resolved":
            skipped["unresolved"] += 1
        elif e.get("partner_specific"):
            skipped["partner_specific"] += 1
        elif (e.get("confidence") or 0) < args.min_confidence:
            skipped["low_confidence"] += 1
        else:
            q, q_hits = redact(scrub_secrets(e["question"]))
            a, a_hits = redact(scrub_secrets(e["answer"]))
            kept.append({**e, "question": q, "answer": a, "internal_hits": sorted(set(q_hits + a_hits))})

    by_api = defaultdict(list)
    for e in kept:
        by_api[e.get("api") or "other"].append(e)

    lines = [f"# FAQ candidates ({len(kept)} of {len(entries)})", "",
             "Skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())), ""]
    for api in sorted(by_api):
        lines += [f"## {api}", ""]
        for e in sorted(by_api[api], key=lambda x: x.get("date") or ""):
            flag = f"  **INTERNAL HITS: {', '.join(e['internal_hits'])}**" if e["internal_hits"] else ""
            lines += [f"### {e['id']} ({e.get('date')}, {e.get('environment')}, conf {e.get('confidence')}){flag}",
                      f"Q: {e['question']}", f"A: {e['answer']}", ""]
    out = base / "faq_candidates.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    (base / "faq_candidates.json").write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(kept)} candidates -> {out}")
    print("skipped:", dict(skipped))
    print("with internal hits:", sum(1 for e in kept if e["internal_hits"]))


if __name__ == "__main__":
    main()
