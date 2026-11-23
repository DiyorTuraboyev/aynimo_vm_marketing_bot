"""Entry point for hosting panels (bot-hosting.net / Pterodactyl start "app.py").

Locally you can also run:  python app.py   or   python -m vm_marketing_bot
"""
import asyncio
import sys

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is required. Choose a newer Python version in the hosting panel.")

from vm_marketing_bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
