"""Check the order-log lookup by hand: the exact block the bot would give the AI.

    python scripts/check_order_logs.py 332481535602757632            # default index
    python scripts/check_order_logs.py SP83281753 --team <team-slug>   # that team's index

Needs LOGS_URL (or ELASTICSEARCH_URL) and LOGS_API_KEY in .env, and the VPN if the log search is
internal. Lines are already scrubbed of tokens, emails and phone numbers.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, order_logs  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("order_id")
    ap.add_argument("--team", default=None)
    args = ap.parse_args()
    if not order_logs.enabled_for(args.team):
        sys.exit("Order lookup is off: set LOGS_URL (or ELASTICSEARCH_URL) and LOGS_API_KEY in .env"
                 + (f", and add '{args.team}' to ORDER_LOOKUP_TEAMS" if config.ORDER_LOOKUP_TEAMS else ""))
    index = order_logs.index_for(args.team)
    print(f"Searching {config.ELASTICSEARCH_URL or config.LOGS_URL} index {index!r}, "
          f"last {config.LOGS_LOOKBACK_DAYS} days\n")
    print(order_logs.logs_block(args.order_id, index))


if __name__ == "__main__":
    main()
