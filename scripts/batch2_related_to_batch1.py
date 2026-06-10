"""Find batch2 scripts related (not exact) to batch 1 skipped questions."""
from __future__ import annotations

import json
import re
from pathlib import Path

BATCH1 = {
    "1259": ("May offer winners announcement (تساهيل)", "إمتى هيتم السحب؟"),
    "525": ("هل يمكن استخدام الرصيد في حساب حالا تحويش في دفع الأقساط او شراء منتجات ؟", "هل يمكن استخدام الرصيد في حساب حالا تحويش في دفع الأقساط او شراء منتجات ؟"),
    "546": ("او انه مضى العقد والحساب ماتفعلش بناخد من العميل البيانات دى", "او انه مضى العقد والحساب ماتفعلش بناخد من العميل البيانات دى"),
    "550": ("ازاي اقدر اعمل ايداع في حساب حالا تحويش؟", "ازاي اقدر اعمل ايداع في حساب حالا تحويش؟"),
    "559": ("ينفع احول مبلغ من محفظه حالا ل حالا تحويش ؟", "ينفع احول مبلغ من محفظه حالا ل حالا تحويش ؟"),
    "491": ("Delay receiving statement ( افادة)", "(Tasaheel) شكوي عميل من تأخير استلام الإفادة"),
    "1637": ("Complaint of pay installments and not reflected", "شكوي مالية لرشاوي ,أقساط تم دفعها للأخصائي ولم تسدد-مخالفات"),
    "1581": ("Request follow up – Confirmation (Halan)", "عميل يستفسر عن الطلب المقدم من خلال على التطبيق"),
    "1258": ("May offer winners announcement (تساهيل)", "هتتواصلوا معايا إزاي لو كسبت؟"),
    "862": ("promocode Wheel of Fortune", None),
    "699": ("installment didn't deducted from salary", "عميل يشكوا ان القسط متخصمش من المرتب الشهري"),
    "1005": ("Amount Deducted One Card serial", "voucherعميل يشكوا ان  الفلوس اتخصمت"),
    "1083": ("ايه هو اقل مبلغ ممكن اكسبه؟", None),
}

RELATED = [
    ("546", 25, "Signed contract / account not active vs heading to branch not activated"),
    ("699", 31, "Salary lending: NOT deducted vs deducted but not on app (opposite cases)"),
    ("550", 20, "General deposit question vs Instapay deposit steps"),
    ("550", 21, "General deposit question vs bank deposit steps"),
    ("550", 22, "General deposit question vs how to save (احوش)"),
    ("559", 22, "Wallet transfer to Tahweesh vs how to save"),
    ("1083", 18, "Min amount to earn vs what is Tahweesh service"),
    ("1083", 19, "Min amount vs guarantees"),
    ("1258", None, "May offer - no batch2 script"),
    ("1259", None, "May offer - no batch2 script"),
]


def main() -> None:
    batch2 = json.loads(Path(__file__).parent.joinpath("batch2_scripts.json").read_text(encoding="utf-8"))
    by_n = {x["n"]: x for x in batch2}
    lines = ["=== RELATED (manual review) ===", ""]
    for pid, n, note in RELATED:
        if n:
            item = by_n[n]
            lines.append(f"Batch1 {pid} ~ Batch2 #{n}")
            lines.append(f"  B1 EN: {BATCH1[pid][0][:90]}")
            lines.append(f"  B2 EN: {item['q_en'][:90]}")
            lines.append(f"  Note: {note}")
        else:
            lines.append(f"Batch1 {pid}: {note}")
        lines.append("")
    Path(__file__).parent.joinpath("batch2_related_report.txt").write_text("\n".join(lines), encoding="utf-8")
    print("wrote batch2_related_report.txt")


if __name__ == "__main__":
    main()
