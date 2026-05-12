"""
Redis queue worker: consume jobs pushed by EmbeddingService.ingest / reindex when REDIS_URL is set.

Run: python -m embedding_service.worker
"""

from __future__ import annotations

import logging
import sys
import time

from embedding_service.config import get_settings
from embedding_service.service import EmbeddingService
from embedding_service.workers.redis_queue import blocking_pop_json

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, stream=sys.stdout)


def main() -> None:
    settings = get_settings()
    if not settings.redis_url:
        _log.error("REDIS_URL is not set; nothing to consume.")
        sys.exit(1)
    svc = EmbeddingService(settings)
    _log.info("Worker listening on queue %s", settings.redis_job_queue)
    try:
        while True:
            payload = blocking_pop_json(
                settings.redis_url,
                settings.redis_job_queue,
                timeout=30,
            )
            if payload is None:
                continue
            _log.info("Processing job %s", payload.get("job_id_hex"))
            try:
                svc.process_job_payload(payload)
            except Exception:
                _log.exception("Unhandled job error")
            time.sleep(0.05)
    finally:
        svc.close()


if __name__ == "__main__":
    main()
