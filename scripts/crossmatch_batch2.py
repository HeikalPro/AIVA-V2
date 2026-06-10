"""Cross-match batch 1 skipped IDs vs batch 2 scripts; scan DB for fillable rows."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.config import get_settings
from embedding_service.db.manager import DatabaseManager
from embedding_service.util.uuids import hex_to_bytes

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

BATCH1 = {
    "1259": ("May offer winners announcement (تساهيل)", "إمتى هيتم السحب؟"),
    "525": ("هل يمكن استخدام الرصيد في حساب حالا تحويش في دفع الأقساط او شراء منتجات ؟", "هل يمكن استخدام الرصيد في حساب حالا تحويش في دفع الأقساط او شراء منتجات ؟"),
    "546": ("او انه مضى العقد والحساب ماتفعلش بناخد من العميل البيانات دى", "او انه مضى العقد والحساب ماتفعلش بناخد من العميل البيانات دى"),
    "550": ("ازاي اقدر اعمل ايداع في حساب حالا تحويش؟", "ازاي اقدر اعمل ايداع في حساب حالا تحويش؟"),
    "559": ("ينفع احول مبلغ من محفظه حالا ل حالا تحويش ؟", "ينفع احول مبلغ من محفظه حالا ل حالا تحويش ؟"),
    "491": ("Delay receiving statement ( افادة)", "(Tasaheel) شكوي عميل من تأخير استلام الإفادة"),
    "1637": ("Complaint of pay installments and not reflected , Complaint of loan officer رشوي ,مخالفات", "شكوي مالية لرشاوي ,أقساط تم دفعها للأخصائي ولم تسدد-مخالفات"),
    "1581": ("Request follow up – Confirmation (Halan)", "عميل يستفسر عن الطلب المقدم من خلال على التطبيق (موجود على ادمن تول)"),
    "1258": ("May offer winners announcement (تساهيل)", "هتتواصلوا معايا إزاي لو كسبت؟"),
    "862": ("عميل بيشتكي من promocode كسبه من عجله الحظ complains about a promo code he won from the Wheel of Fortune", None),
    "699": ("installment didn't deducted from salary (Salary Lending)", "عميل يشكوا ان القسط متخصمش من المرتب الشهري (Salary Lending)"),
    "1005": ("Amount Deducted and Customer didn't receive the Serial Number (One Card)", "voucherعميل يشكوا ان  الفلوس اتخصمت و لم يستقبل ال (One Card)"),
    "1083": ("ايه هو اقل مبلغ ممكن اكسبه؟", None),
}


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.strip())


def _read_lob(v) -> str:
    if v is None:
        return ""
    return v.read() if hasattr(v, "read") else str(v)


def main() -> None:
    batch2_path = Path(__file__).resolve().parent / "batch2_scripts.json"
    batch2 = json.loads(batch2_path.read_text(encoding="utf-8"))
    by_ar = {_norm(x["q_ar"]): x for x in batch2 if x.get("q_ar")}
    by_en = {_norm(x["q_en"]): x for x in batch2 if x.get("q_en")}

    lines = ["=== BATCH 1 vs BATCH 2 ===\n"]
    exact = []
    related = []

    for pid, (q_en, q_ar) in BATCH1.items():
        hit = None
        if q_ar and _norm(q_ar) in by_ar:
            hit = ("AR exact", by_ar[_norm(q_ar)]["n"])
        elif q_en and _norm(q_en) in by_en:
            hit = ("EN exact", by_en[_norm(q_en)]["n"])
        if hit:
            exact.append((pid, hit))
            lines.append(f"{pid}: EXACT match batch2 #{hit[1]} ({hit[0]})")
            continue

        # fuzzy related
        rel = []
        for item in batch2:
            if q_en and item.get("q_en") and _norm(q_en) == _norm(item["q_en"]):
                rel.append(f"batch2 #{item['n']} same EN")
            if q_ar and item.get("q_ar"):
                ar = _norm(item["q_ar"])
                qar = _norm(q_ar)
                if qar and ar and (qar in ar or ar in qar):
                    rel.append(f"batch2 #{item['n']} AR overlap")
        if rel:
            related.append((pid, rel))
            lines.append(f"{pid}: RELATED -> {', '.join(rel)}")
        else:
            lines.append(f"{pid}: no match in batch2")

    lines.append(f"\nExact fills possible: {len(exact)}")
    lines.append(f"Related only: {len(related)}")

    # scan all skipped in DB vs full batch2 json (subset) - load from md later
    lines.append("\n=== NOTE ===")
    lines.append("batch2_scripts.json currently has sample entries only.")
    lines.append("Expand JSON with all 38 scripts to auto-fill DB.")

    out = Path(__file__).resolve().parent / "batch_crossmatch_report.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
