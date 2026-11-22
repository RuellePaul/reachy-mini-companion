"""`python -m robot_agent`: the .env and the SDK floor first, then the robot."""

import asyncio
import contextlib
import logging

from robot_agent import boot

boot.load_env()
boot.require_genai()

from robot_agent import main  # config reads the environment loaded above

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
for noisy in ('httpx', 'httpcore', 'urllib3', 'websockets', 'google_genai'):
    logging.getLogger(noisy).setLevel(logging.WARNING)
with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(main.main_async())
