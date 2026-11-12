"""Retrain the local FAQ model by hand: the same pipeline the scheduled job runs
(bot/local_model_train.py), with a printed report.

    python scripts/train_local_model.py                  # retrain now and save if it passes
    python scripts/train_local_model.py --no-teacher     # no AI calls: saved practice data only
    python scripts/train_local_model.py --dry-run        # report only, don't save
    python scripts/train_local_model.py --target 0.94    # other precision target

The server retrains on its own (daily + after FAQ changes), so this is only for trying settings.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, local_model_train  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-teacher", action="store_true", help="no AI calls: use saved practice data and labels only")
    ap.add_argument("--dry-run", action="store_true", help="report only, don't save the model")
    ap.add_argument("--target", type=float, default=config.LOCAL_MODEL_TARGET_PRECISION)
    args = ap.parse_args()
    try:
        import sklearn  # noqa: F401
    except ImportError:
        sys.exit("Training needs scikit-learn: pip install -r requirements.txt")
    print(f"AI teacher: {config.PROVIDER}")
    outcome, message, report = asyncio.run(local_model_train.run(
        "script", lambda m: print(f"  {m}", flush=True), use_teacher=not args.no_teacher,
        dry_run=args.dry_run, target=args.target, force=True))
    print(f"\n{outcome}: {message}")
    if not report:
        return
    names = {"overall": "all examples", "real": "real questions", "paraphrase": "paraphrases", "near_miss": "near misses"}
    print("\nCross-validation at the chosen threshold:")
    for key, name in names.items():
        if m := report.get(key):
            print(f"  {name:15} answered {m['answered']:4}/{m['examples']:<4} precision {m['precision']:.1%}  "
                  f"coverage of FAQ-type questions {m['coverage']:.1%}  wrong {m['wrong_answers']}")
    print("\nTrade-off for other thresholds (move it with --target / LOCAL_MODEL_TARGET_PRECISION):")
    for row in report["curve"]:
        a, r = row["all"], row["real"]
        print(f"  {row['threshold']:.1f}: all precision {a['precision']:.1%} coverage {a['coverage']:.1%}"
              + (f" | real precision {r['precision']:.1%} coverage {r['coverage']:.1%}" if r else ""))
    for w in report["wrong"]:
        print(f"  wrong: {w['true']!r} -> {w['picked']!r} (p={w['p']})")
    if "export_check" in report:
        print(f"\nSaved model matches scikit-learn on {report['export_check']:.1%} of training questions")


if __name__ == "__main__":
    main()
