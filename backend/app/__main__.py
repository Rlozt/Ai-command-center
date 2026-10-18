"""`python -m app` starts the server on loopback."""
import uvicorn

from .config import load_config


def main() -> None:
    cfg = load_config()
    print(f"AI Command Center ({cfg.mode} mode) on http://{cfg.host}:{cfg.port}  (Ctrl+C to stop)")
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding to a non-loopback address exposes this app to your network.")
    uvicorn.run("app.api:app_factory", host=cfg.host, port=cfg.port, factory=True, log_level="info")


if __name__ == "__main__":
    main()
