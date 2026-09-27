"""Text helpers shared by the CRM extractors and validators: digit and label
normalisation plus the deterministic patterns for emails, phones and URLs."""

from __future__ import annotations

import re
import unicodedata

# Arabic-Indic (U+0660..0669) and Extended Arabic-Indic / Persian (U+06F0..06F9) digits.
_DIGITS = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGITS.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_DIGITS.update({ord("٫"): ".", ord("٬"): ","})  # Arabic decimal and thousands separators

_ARABIC_MARKS = re.compile("[\u0640\u064B-\u065F\u0670]")  # tatweel and harakat
_LABEL_PUNCT = re.compile(r"[\s_\-./#№:：()\[\]'\"*]+")

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)
URL_RE = re.compile(r"(?<![\w@/])(?:https?://|www\.)[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
BARE_URL_RE = re.compile(
    r"^(?:https?://)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}(?::\d+)?(?:[/?#]\S*)?$"
)
PHONE_RE = re.compile(r"(?<![\w+])(?:\+|00)?\(?\d[\d\s().-]{5,20}\d(?![\w])")
_DATE_LIKE = re.compile(r"^\s*(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*$")
_TRAILING = ".,;:!?)]}>'\""


def normalize_digits(text: str) -> str:
    """Replace Arabic-Indic digits and separators with ASCII ones."""
    return text.translate(_DIGITS)


def normalize_label(text: str) -> str:
    """A comparison key for labels and aliases: NFKC, casefolded, Arabic marks removed,
    punctuation and underscores collapsed to single spaces."""
    text = _ARABIC_MARKS.sub("", unicodedata.normalize("NFKC", text)).casefold()
    return _LABEL_PUNCT.sub(" ", text).strip()


def strip_trailing_punct(text: str) -> str:
    """Trailing sentence punctuation is almost never part of an email, URL or phone."""
    return text.rstrip(_TRAILING)


def phone_digits(text: str) -> str:
    """The digits of a phone number, with a leading '+' kept."""
    t = normalize_digits(text).strip()
    digits = re.sub(r"\D", "", t)
    if t.startswith("00"):
        return "+" + digits[2:]
    return ("+" if t.startswith("+") else "") + digits


def is_plausible_phone(candidate: str, *, strict: bool) -> bool:
    """8..15 digits, not a date. `strict` (for unlabeled text) also requires an
    international prefix, a leading 0, or visible grouping, so bare IDs are not phones."""
    cand = normalize_digits(candidate).strip()
    if _DATE_LIKE.match(cand):
        return False
    n = len(re.sub(r"\D", "", cand))
    if not 8 <= n <= 15:
        return False
    if not strict:
        return True
    if cand.startswith(("+", "00", "0", "(")):
        return True
    return bool(re.search(r"\d[\s().-]\d", cand))


def find_emails(text: str) -> list[tuple[str, int]]:
    """(email, start offset) pairs in `text`."""
    return [(strip_trailing_punct(m.group(0)), m.start()) for m in EMAIL_RE.finditer(text)]


def find_urls(text: str) -> list[tuple[str, int]]:
    """(url, start offset) pairs; only explicit http(s):// or www. forms."""
    return [(strip_trailing_punct(m.group(0)), m.start()) for m in URL_RE.finditer(text)]


def find_phones(text: str, *, strict: bool = True) -> list[tuple[str, int]]:
    """(phone as written, start offset) pairs, Arabic-Indic digits normalised."""
    norm = normalize_digits(text)
    out: list[tuple[str, int]] = []
    for m in PHONE_RE.finditer(norm):
        cand = m.group(0).strip()
        if cand.count("(") != cand.count(")"):
            cand = cand.strip("()").strip()
        if is_plausible_phone(cand, strict=strict):
            out.append((cand, m.start()))
    return out


def looks_like_url(text: str) -> bool:
    """A URL with or without scheme (``acme.com/about``)."""
    return bool(BARE_URL_RE.match(text.strip())) and "@" not in text
