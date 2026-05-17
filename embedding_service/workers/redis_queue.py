from __future__ import annotations

import json
import logging
from typing import Any

import redis

_log = logging.getLogger(__name__)


def push_json_job(redis_url: str, queue_name: str, payload: dict[str, Any]) -> None:
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        r.rpush(queue_name, json.dumps(payload, ensure_ascii=False))
    finally:
        r.close()


def blocking_pop_json(redis_url: str, queue_name: str, timeout: int = 5) -> dict[str, Any] | None:
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        item = r.blpop(queue_name, timeout=timeout)
        if not item:
            return None
        _, raw = item
        return json.loads(raw)
    finally:
        r.close()
