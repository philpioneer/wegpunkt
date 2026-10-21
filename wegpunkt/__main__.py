import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "wegpunkt.app:create_app",
        factory=True,
        host=os.environ.get("WEGPUNKT_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEGPUNKT_PORT", "8000")),
        proxy_headers=True,
    )
