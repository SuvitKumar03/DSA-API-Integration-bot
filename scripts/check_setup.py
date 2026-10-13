"""Check that this machine is ready to run the assistant, and say how to fix what isn't.

Run from the repo root, with the virtual environment active:
    python scripts/check_setup.py
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

problems = 0


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def bad(msg: str, fix: str) -> None:
    global problems
    problems += 1
    print(f"  ❌ {msg}\n     → {fix}")


def warn(msg: str, fix: str) -> None:
    print(f"  ⚠️  {msg}\n     → {fix}")


def check_python() -> bool:
    print("Python and packages")
    if sys.version_info < (3, 10):
        bad(f"Python {sys.version.split()[0]}", "Install Python 3.10 or newer")
        return False
    ok(f"Python {sys.version.split()[0]}")
    if sys.prefix == sys.base_prefix:
        warn("Not running inside the virtual environment", "Run: source .venv/bin/activate")
    missing = []
    for module in ("fastapi", "uvicorn", "anthropic", "openai", "dotenv", "google.auth"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        bad(f"Missing packages: {', '.join(missing)}", "Run: pip install -r requirements.txt")
        return False
    ok("Packages installed")
    return True


def check_files() -> None:
    print("Private files")
    docs = [p for p in (ROOT / "knowledge").glob("*.md") if p.name != "README.md"]
    if docs:
        ok(f"{len(docs)} knowledge files in knowledge/")
    else:
        bad("No knowledge files", "Get private-files.zip from the team and run: unzip private-files.zip")
    if (ROOT / "internal_terms.txt").exists():
        ok("internal_terms.txt present")
    else:
        warn("internal_terms.txt missing (internal names won't be filtered)",
             "It comes in private-files.zip. Required before sharing with partners")
    if (ROOT / ".env").exists():
        ok(".env present")
    else:
        bad("No .env file", "Run: cp .env.example .env  and choose a PROVIDER")


def check_claude_code(config) -> None:
    from bot.engine import _API_AUTH_VARS, claude_code_bin, claude_code_hint

    binary = claude_code_bin()
    if not binary:
        bad("Claude Code CLI not found",
            "Install: curl -fsSL https://claude.ai/install.sh | bash  (or npm install -g @anthropic-ai/claude-code), "
            "then run `claude` once to sign in")
        return
    version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
    ok(f"Claude Code found: {binary} ({version})")
    if os.environ.get("ANTHROPIC_API_KEY"):
        warn("ANTHROPIC_API_KEY is set in your shell", "Fine: the app hides it so Claude Code uses your subscription")
    print("     Sending a tiny test question to your Claude (takes a few seconds)...")
    env = {k: v for k, v in os.environ.items() if k not in _API_AUTH_VARS}
    try:
        run = subprocess.run(
            [binary, "-p", "Reply with just the word OK", "--output-format", "json", "--tools", "",
             "--strict-mcp-config", "--no-session-persistence", "--model", config.CLAUDE_CODE_MODEL],
            capture_output=True, text=True, timeout=120, env=env, cwd=Path.home())
    except subprocess.TimeoutExpired:
        bad("Claude Code didn't answer within 2 minutes", "Run `claude` in a terminal and check it works")
        return
    try:
        result = json.loads(run.stdout or "{}")
    except ValueError:
        result = {"is_error": True, "result": run.stdout[-300:]}
    if result.get("is_error") or not result.get("result"):
        detail = f"{result.get('result', '')} {run.stderr[-300:]}"
        bad(f"Claude Code failed: {detail.strip()[:200]}", claude_code_hint(detail))
    else:
        ok(f"Your Claude subscription answers (model {config.CLAUDE_CODE_MODEL})")


def check_api(config) -> None:
    from bot.engine import complete

    if config.PROVIDER == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        bad("ANTHROPIC_API_KEY is empty", "Add the key to .env, or use PROVIDER=claude_code with your own Claude")
        return
    if config.PROVIDER == "openai_compatible" and not (config.LLM_BASE_URL and config.LLM_API_KEY and config.LLM_MODEL):
        bad("LLM_BASE_URL, LLM_API_KEY or LLM_MODEL is empty", "See the Gemini block in docs/LOCAL_SETUP.md")
        return
    print("     Sending a tiny test question (takes a few seconds)...")
    answer = asyncio.run(complete("Reply with just the word OK.", "ping", max_tokens=20))
    if answer:
        ok(f"{config.PROVIDER} answers (model {config.MODEL})")
    else:
        bad(f"{config.PROVIDER} didn't answer", "Check the key and model in .env; the error is printed above")


def main() -> None:
    print("\nChecking your setup\n")
    if not check_python():
        sys.exit(1)
    check_files()
    from bot import config

    print(f"AI provider: PROVIDER={config.PROVIDER}")
    if config.PROVIDER == "claude_code":
        check_claude_code(config)
    elif config.PROVIDER in ("anthropic", "openai_compatible"):
        check_api(config)
    else:
        bad(f"Unknown PROVIDER '{config.PROVIDER}'", "Use claude_code, anthropic or openai_compatible")

    print()
    if problems:
        print(f"{problems} problem(s) to fix, then run this again.")
        sys.exit(1)
    print("All good. Start the server:  uvicorn app.main:app --port 8000   then open http://localhost:8000")


if __name__ == "__main__":
    main()
