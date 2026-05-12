"""Rough token estimates when the provider does not return usage (e.g. Oracle in-DB)."""


def estimate_tokens_char_heuristic(texts: list[str]) -> int:
    """
    ~4 characters per token is a common coarse rule for Latin script;
    acceptable as a fallback for mixed AR/EN when no tokenizer is available.
    """
    total = 0
    for t in texts:
        if not t:
            continue
        total += max(1, len(t) // 4)
    return total
