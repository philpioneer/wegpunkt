import logging

import uvicorn

from .config import merged_env

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    env = merged_env()
    uvicorn.run(
        "arriba.app:create_app",
        factory=True,
        host=env.get("ARRIBA_HOST", "127.0.0.1"),
        port=int(env.get("ARRIBA_PORT", "8000")),
        proxy_headers=True,
    )
