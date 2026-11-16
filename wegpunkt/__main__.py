import logging
import os
from pathlib import Path

import uvicorn

from .config import read_env_file

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    env = {**read_env_file(Path(".env")), **os.environ}
    uvicorn.run(
        "wegpunkt.app:create_app",
        factory=True,
        host=env.get("WEGPUNKT_HOST", "127.0.0.1"),
        port=int(env.get("WEGPUNKT_PORT", "8000")),
        proxy_headers=True,
    )
