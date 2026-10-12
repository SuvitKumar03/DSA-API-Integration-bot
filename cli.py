"""Chat with the bot in the terminal - quick way to check answer quality.

Usage:  python cli.py            (interactive)
        python cli.py "question"  (single question)
"""

import asyncio
import sys

from bot.engine import stream_answer


async def answer(history: list[dict]) -> str:
    usage: dict = {}
    parts = []
    async for chunk in stream_answer(history, usage_out=usage):
        print(chunk, end="", flush=True)
        parts.append(chunk)
    print(f"\n\n[tokens in={usage.get('input_tokens')} cached={usage.get('cache_read_input_tokens')} "
          f"cache_write={usage.get('cache_creation_input_tokens')} out={usage.get('output_tokens')}]\n")
    return "".join(parts)


async def main() -> None:
    if len(sys.argv) > 1:
        await answer([{"role": "user", "content": " ".join(sys.argv[1:])}])
        return
    history: list[dict] = []
    print("Ask a question (Ctrl+C to quit).\n")
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            continue
        history.append({"role": "user", "content": q})
        print("bot> ", end="")
        history.append({"role": "assistant", "content": await answer(history)})


if __name__ == "__main__":
    asyncio.run(main())
