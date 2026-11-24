"""python -m mac_server: the Mac companion server on 0.0.0.0:$MAC_AGENT_PORT (7777)."""

from __future__ import annotations

import logging

import uvicorn

from mac_server.config import load_config
from mac_server.server import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    config = load_config()
    uvicorn.run(
        create_app(config),
        host='0.0.0.0',
        port=config.port,
        # X-Forwarded-For is believed from this Mac only: what the Vite proxy forwards keeps its real client.
        proxy_headers=True,
        forwarded_allow_ips='127.0.0.1',
        ws='websockets-sansio',  # /api/ws; without the library the boot fails instead of 404-ing the dashboard
        timeout_graceful_shutdown=1,  # the robot's long-poll is always parked: cancel it, do not wait 20 s
        access_log=False,  # the robot posts every transcript delta; the server logs what matters itself
    )


if __name__ == '__main__':
    main()
