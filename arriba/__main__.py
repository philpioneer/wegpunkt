import logging

import uvicorn

from .config import merged_env

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # nicht jede geladene Kartenkachel melden
    env = merged_env()
    uvicorn.run(
        "arriba.app:create_app",
        factory=True,
        host=env.get("ARRIBA_HOST", "127.0.0.1"),
        port=int(env.get("ARRIBA_PORT", "8000")),
        proxy_headers=True,
        # Von welchen Adressen X-Forwarded-For gilt. Hinter dem Proxy eines Hosters: "*"
        forwarded_allow_ips=env.get("ARRIBA_FORWARDED_IPS", "127.0.0.1"),
    )
