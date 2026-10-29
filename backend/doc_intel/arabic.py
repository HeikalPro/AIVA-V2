"""Arabic text-layer quality check: pure functions, standard library only.

Some PDF producers write the ToUnicode map of the lam-alef ligature with its two
letters swapped. The page renders correctly, but the text layer then says "ال"
(alef + lam) wherever the document says "لا" (lam + alef), and nobody notices until
the extracted text is searched. ``AIVA-V2/test.py`` found this in "6 Digits.pdf";
this module is its heuristic, made importable and testable.

Why it works:

- lam + alef is very common in real Arabic: "لا", "إلا", "خلال", "الاتصال", ...
- A word ENDING in alef + lam is much rarer ("مجال", "سؤال", "أعمال").
- In a reversed layer every lam-alef turns into alef-lam. Words containing "لا"
  disappear, and words ending in "ال" multiply ("لا" -> "ال", "إلا" -> "إال").

Measured on "6 Digits.pdf": the pdfium text layer scores correct=0 / broken=73,
while the OCR'd (correct) text of the same document scores correct=69 / broken=20.
The rule keeps ``test.py``'s thresholds (broken >= 5 and broken > correct). The only
change is that diacritics and tatweel are removed before counting, because a fatha
between lam and alef ("لَا") would otherwise hide a correct pair.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

LAM = "\u0644"  # ARABIC LETTER LAM
ALEF = "\u0627"  # ARABIC LETTER ALEF
LAM_ALEF = LAM + ALEF
ALEF_LAM = ALEF + LAM

# Below this many broken words the counts are noise (a heading, a name).
MIN_BROKEN = 5

_ARABIC_WORD = re.compile(r"[\u0600-\u06FF]+")
# Harakat, superscript alef and tatweel: they sit between letters without changing them.
_MARKS = re.compile(r"[\u064B-\u065F\u0670\u0640]")


@dataclass(frozen=True)
class LamAlefScore:
    words: int  # Arabic words looked at
    correct: int  # words containing lam+alef in logical order
    broken: int  # words ending in alef+lam

    @property
    def reversed(self) -> bool:
        """True when the text layer looks like it stores lam-alef pairs backwards."""
        return self.broken >= MIN_BROKEN and self.broken > self.correct

    def as_dict(self) -> dict[str, Any]:
        return {"words": self.words, "correct": self.correct, "broken": self.broken, "reversed": self.reversed}


def lam_alef_score(text: str) -> LamAlefScore:
    """Count correct (contains "لا") and broken (ends with "ال") Arabic words in ``text``."""
    words = _ARABIC_WORD.findall(_MARKS.sub("", text or ""))
    correct = sum(1 for w in words if LAM_ALEF in w)
    broken = sum(1 for w in words if w.endswith(ALEF_LAM))
    return LamAlefScore(words=len(words), correct=correct, broken=broken)


def text_layer_reversed(text: str) -> bool:
    """Whether ``text`` (a PDF text layer) has its lam-alef ligatures stored reversed."""
    return lam_alef_score(text).reversed
