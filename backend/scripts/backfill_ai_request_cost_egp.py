"""One-time backfill: recompute AIVA_ai_requests.total_cost in EGP from SovereignEG prices.

Existing rows were stored with the old USD placeholder rates. This recomputes each row's
cost using the live SovereignEG EGP per-model prices (plus the configured markup), so the
Total-cost card totals are consistent with new requests.

Run from the repo root (AIVA-V2):
    python -m backend.scripts.backfill_ai_request_cost_egp            # apply
    python -m backend.scripts.backfill_ai_request_cost_egp --dry-run  # preview only

Rows with no model_name or zero tokens are skipped (their cost is left as-is / NULL).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LLM_SERVICE = _ROOT / "llm_service"
for _p in (_ROOT, _LLM_SERVICE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backend.config import get_settings
from backend.database import Database
from backend.services.sovereign_catalog import estimate_llm_cost_egp, get_sovereign_catalog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("backfill_cost_egp")


async def main(dry_run: bool) -> None:
    settings = get_settings()
    db = Database(settings)
    await db.init_pool()
    try:
        catalog = await get_sovereign_catalog(force=True)
        if not catalog:
            _log.error("SovereignEG catalog is empty/unavailable — aborting (no prices to apply).")
            return
        _log.info("Loaded %d SovereignEG models", len(catalog))

        rows = await db.fetch_all(
            """
            SELECT id, model_name, input_tokens, output_tokens, total_cost
            FROM AIVA_ai_requests
            WHERE model_name IS NOT NULL
              AND (NVL(input_tokens, 0) > 0 OR NVL(output_tokens, 0) > 0)
            """
        )
        _log.info("Rows to consider: %d", len(rows))

        updated = skipped = 0
        for r in rows:
            cost = await estimate_llm_cost_egp(
                model_name=str(r["model_name"]),
                input_tokens=r.get("input_tokens"),
                output_tokens=r.get("output_tokens"),
                settings=settings,
            )
            if cost is None:
                skipped += 1  # model not in catalog → no price
                continue
            if dry_run:
                _log.info("[dry-run] id=%s %s: %s -> E£%s", r["id"], r["model_name"], r.get("total_cost"), cost)
                updated += 1
                continue
            await db.execute(
                "UPDATE AIVA_ai_requests SET total_cost = :c WHERE id = :id",
                {"c": cost, "id": int(r["id"])},
            )
            updated += 1

        _log.info("Done. %s=%d  skipped(no pricing)=%d", "would_update" if dry_run else "updated", updated, skipped)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
