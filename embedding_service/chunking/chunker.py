from __future__ import annotations


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Greedy character windows with overlap; prefers splitting on blank lines then newlines."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = min(i + max_chars, n)
        piece = text[i:j]
        if j < n:
            cut = piece.rfind("\n\n")
            if cut < max_chars // 4:
                cut = piece.rfind("\n")
            if cut >= max_chars // 4:
                piece = piece[: cut + 1]
                j = i + len(piece)
        piece = piece.strip()
        if piece:
            chunks.append(piece)
        if j >= n:
            break
        next_i = j - overlap
        if next_i <= i:
            next_i = i + 1
        i = next_i
    return chunks
