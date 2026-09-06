"""Smoke-test 6-digits retrieval: run agent-style questions through the real search path."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embedding_service.service import EmbeddingService

CORPUS_HEX = "091B8D61C54645EF86DF0D78E0B9AE0C"

QUERIES = [
    "العميل نسي كلمة السر 6 أرقام الخاصة بكارت حالاً على التطبيق",
    "تم إيقاف حسابك بعد إدخال كلمة سر غير صحيحة عدة مرات",
    "ما الفرق بين كلمة السر 6 أرقام و 4 أرقام؟",
    "customer forgot the 6 digit app password",
    "العميل الكارت بتاعه ضاع",
]

def main() -> None:
    svc = EmbeddingService()
    try:
        for q in QUERIES:
            hits = svc.search(CORPUS_HEX, q, top_k=3, verticals=["6-digits"])
            print(f"\nQ: {q}")
            for h in hits:
                text = h.get("chunk_text") or h.get("text") or ""
                pid = h.get("parent_id") or h.get("external_parent_id")
                score = h.get("score")
                flags = []
                if "سرية البيانات" in text or "متشاركش" in text:
                    flags.append("confidentiality")
                if "السيرفي" in text:
                    flags.append("survey")
                s = f"{score:.3f}" if isinstance(score, (int, float)) else score
                print(f"   {pid}  score={s}  {'+'.join(flags) or '-'}")
    finally:
        svc.close()


if __name__ == "__main__":
    main()
