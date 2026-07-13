#!/usr/bin/env python3
"""Smoke-test the widget logging endpoint (POST /api/logs/widget-turn).

Sends a fake widget turn so you can confirm the wiring end-to-end before
pointing the real widget at it. Uses only the Python standard library.

Usage (PowerShell):
    $env:WIDGET_LOG_SECRET = "your-secret"
    python scripts/test_widget_log.py --url http://localhost:8000/api/logs/widget-turn

Or pass everything explicitly:
    python scripts/test_widget_log.py --url http://localhost:8000/api/logs/widget-turn --secret your-secret --corpus-id <hex>

On success the endpoint returns {"ok": true} and a WIDGET-tagged row should
appear in Logs -> RAG retrieval and Logs -> AI requests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _build_sample(corpus_id: str | None) -> dict:
    return {
        "corpus_id": corpus_id,
        "query_text": "SMOKE TEST: how do I reset my password?",
        "top_k": 5,
        "verticals": None,
        "chunks": [
            {"parent_id": "1495", "chunk_index": 0, "score": 0.87, "text": "To reset your password, open Settings > Security..."},
            {"parent_id": "1501", "chunk_index": 2, "score": 0.71, "text": "Password rules: at least 8 characters..."},
        ],
        "model_name": "gpt-4.1-mini",
        "provider": "openai",
        "input_tokens": 812,
        "output_tokens": 143,
        "response_time_ms": 1240,
        "total_cost": None,  # let the backend estimate from the model + tokens
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the widget logging endpoint.")
    parser.add_argument(
        "--url",
        default=os.environ.get("AIVA_BACKEND_LOG_URL", "http://localhost:8000/api/logs/widget-turn"),
        help="Full endpoint URL (default: env AIVA_BACKEND_LOG_URL or localhost).",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("WIDGET_LOG_SECRET") or os.environ.get("AIVA_LOG_SECRET", ""),
        help="Shared secret (default: env WIDGET_LOG_SECRET / AIVA_LOG_SECRET).",
    )
    parser.add_argument(
        "--corpus-id",
        default=os.environ.get("AIVA_CORPUS_ID"),
        help="Corpus id (hex) so the row resolves to an account; optional.",
    )
    args = parser.parse_args()

    if not args.secret:
        print("ERROR: no secret provided. Set WIDGET_LOG_SECRET or pass --secret.", file=sys.stderr)
        return 2

    payload = json.dumps(_build_sample(args.corpus_id)).encode("utf-8")
    req = urllib.request.Request(
        args.url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Widget-Log-Secret": args.secret,
        },
    )

    print(f"POST {args.url}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            print(f"HTTP {resp.status}: {body}")
            print("\nOK — check Logs -> RAG retrieval and AI requests for a WIDGET-tagged row.")
            return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        if exc.code == 401:
            print("-> Secret mismatch. Make sure --secret matches the backend's WIDGET_LOG_SECRET.", file=sys.stderr)
        elif exc.code == 503:
            print("-> Widget logging is disabled. Set WIDGET_LOG_SECRET on the backend and restart it.", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Connection failed: {exc.reason}", file=sys.stderr)
        print("-> Is the backend running and the --url correct?", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
