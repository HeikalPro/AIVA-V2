"""Smoke-test 6-digits retrieval: run agent-style questions through the real search path.

Each entry is (question, expected_parent_id). Prints the top hits and whether the
expected record came back, plus which hits carry the closing obligations
(confidentiality script / end-of-call survey).

    python scripts/check_6digits_retrieval.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.service import EmbeddingService

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

# (question, expected parent id or None for "should find nothing useful")
QUERIES: list[tuple[str, str | None]] = [
    # one per record
    ("العميل بيقوله «تم إيقاف حسابك» بعد ما دخل كلمة السر غلط كذا مرة", "1901"),
    ("بيظهر للعميل «حدث خطأ» لما بيدخل على كارت حالاً، وعايز يغير كلمة سر الـ ATM", "1902"),
    ("أملأ إيه في خانة Verification Passed و Call Disconnected في التيكت؟", "1903"),
    ("العميل بيقول حالاً كريديت موقوف وظاهر Is Blocked في الـ CRM", "1904"),
    ("إيه سكريبت سرية البيانات اللي بقوله للعميل؟", "1905"),
    ("المكالمة وصلت غلط على كيو 6 أرقام، أعمل إيه وأملأ التيكت إزاي؟", "1906"),
    ("العميل عنده مشكلة في أوردر طلبه من حالاً جملة، أحوله فين؟", "1907"),
    ("العميل بيسأل عن قسط الجمعية والتحويش، أحوله على إيه؟", "1908"),
    ("العميل مش قادر يشتري بالكارت ومرتبه مش نازل على كارت حالاً", "1909"),
    ("التطبيق مش بيفتح مع العميل خالص", "1910"),
    ("إمتى أراجع بيانات العميل وإيه البيانات الأساسية والإضافية؟", "1911"),
    ("العميل يضغط إيه في الـ IVR علشان يبلغ عن فقدان الكارت؟ وإيه مواعيد العمل؟", "1912"),
    ("العميل نسي كلمة سر الكارت على التطبيق، يقدر يعملها بنفسه إزاي؟", "1913"),
    ("إيه الفرق بين كلمة السر 6 أرقام وكلمة السر 4 أرقام؟", "1914"),
    ("رصيد العميل طالع بالسالب، أقوله إيه؟", "1915"),
    ("العميل استلم الكارت ومش عارف يفعّله", "1916"),
    ("الخطوات اللي أراجعها من السيستم قبل ما أعمل تيكت على Zendesk؟", "1917"),
    # behaviour checks
    ("customer forgot his 6 digit app password, what do I tell him?", "1913"),
    ("العميل الكارت بتاعه ضاع أو اتسرق", "1917"),
    # should NOT be answerable from this queue
    ("إيه سعر الفائدة على تمويل المشروعات؟", None),
]


def main() -> None:
    svc = EmbeddingService()
    passed = failed = 0
    try:
        for question, expected in QUERIES:
            hits = svc.search(CORPUS_HEX, question, top_k=3, verticals=["6-digits"])
            ids = [str(h.get("parent_id") or h.get("external_parent_id")) for h in hits]
            ok = (expected in ids) if expected else True
            passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
            print(f"\n[{'ok ' if ok else 'MISS'}] {question}")
            print(f"       expected={expected or '(nothing specific)'}")
            for h in hits:
                text = h.get("chunk_text") or h.get("text") or ""
                pid = str(h.get("parent_id") or h.get("external_parent_id"))
                score = h.get("score")
                flags = []
                if "سرية البيانات" in text or "متشاركش" in text:
                    flags.append("confidentiality")
                if "السيرفي" in text:
                    flags.append("survey")
                s = f"{score:.3f}" if isinstance(score, (int, float)) else score
                print(f"       {pid}  score={s}  {'+'.join(flags) or '-'}")
        print(f"\n{passed} matched, {failed} missed")
    finally:
        svc.close()


if __name__ == "__main__":
    main()
