"""Run the API with Uvicorn (``aiva-chatbot-api`` console entry)."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    p = argparse.ArgumentParser(description="Run AIVA Chatbot HTTP API.")
    p.add_argument("--host", default=os.environ.get("AIVA_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("AIVA_PORT", "8000")))
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    import uvicorn

    uvicorn.run(
        "aiva_chatbot.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
