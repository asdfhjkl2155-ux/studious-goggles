"""Save the bot's keys into .env without showing them on screen.

    venv/bin/python set_keys.py

Asks for the Telegram bot token and the fal key (input is hidden), checks their format,
and writes them to .env. Press Enter on an empty prompt to keep the current value.
"""

from __future__ import annotations

import getpass
import re
import shutil
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

ENV = Path(__file__).resolve().parent / ".env"
EXAMPLE = ENV.with_name(".env.example")

KEYS = [
    # (name, prompt, pattern, where to get it)
    ("TELEGRAM_BOT_TOKEN", "token البوت من BotFather", r"\d{5,}:[A-Za-z0-9_-]{30,}", "@BotFather"),
    ("FAL_KEY", "مفتاح fal", r"[A-Za-z0-9_-]{8,}:[A-Za-z0-9_-]{8,}", "fal.ai → Keys"),
]


def clean(value: str) -> str:
    """Remove what tends to come along when copying: spaces, quotes, a "KEY=" prefix."""
    value = value.strip().strip("'\"").strip()
    if "=" in value and value.split("=", 1)[0].isupper():
        value = value.split("=", 1)[1].strip()
    return value


def main() -> int:
    if not ENV.exists():
        shutil.copy(EXAMPLE, ENV)
    current = dotenv_values(ENV)
    print("الصق كل مفتاح ثم اضغط Enter. لن يظهر شيء أثناء اللصق، وهذا طبيعي.")
    print("للإبقاء على القيمة الحالية اضغط Enter فقط.\n")

    for name, label, pattern, source in KEYS:
        status = "محفوظ حالياً" if current.get(name) else "فارغ حالياً"
        while True:
            value = clean(getpass.getpass(f"{label} ({status}): "))
            if not value:
                break
            if re.fullmatch(pattern, value):
                set_key(str(ENV), name, value, quote_mode="never")
                print("  ✅ تم الحفظ")
                break
            print(f"  ❌ هذا لا يبدو {label}. انسخه مرة أخرى من {source} وحاول.")

    final = dotenv_values(ENV)
    print()
    for name, *_ in KEYS:
        print(f"{name}: {'OK' if final.get(name) else 'فارغ'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nتم الإلغاء.")
        sys.exit(1)
