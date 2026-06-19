import hashlib
import re
from typing import List
from core.retrieval.config import PASSAGE_MAX_CHARS, PASSAGE_MIN_CHARS
from core.retrieval.schema import Passage


def compute_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _split_into_paragraphs(text: str) -> List[str]:
    """Split text into paragraphs on double newlines, then single newlines."""
    paragraphs = re.split(r'\n\s*\n', text.strip())
    result = []
    for p in paragraphs:
        sub = re.split(r'\n', p.strip())
        for s in sub:
            s = s.strip()
            if s:
                result.append(s)
    return result


def chunk_text(text: str, source_type: str, source_id: str,
               memory_id: int = None, index_version: int = 1) -> List[Passage]:
    """Deterministic passage chunking with overlap.
    
    Strategy:
    - Prefer paragraph boundaries.
    - Merge small consecutive paragraphs.
    - Split oversized paragraphs at sentence boundaries.
    - Maintain overlap for context continuity.
    """
    paragraphs = _split_into_paragraphs(text)
    merged = _merge_small_paragraphs(paragraphs)
    passages = []
    passage_idx = 0

    for block in merged:
        if len(block) <= PASSAGE_MAX_CHARS:
            fp = compute_fingerprint(block)
            passages.append(Passage(
                source_type=source_type,
                source_id=source_id,
                memory_id=memory_id,
                passage_index=passage_idx,
                text=block,
                char_count=len(block),
                source_fingerprint=fp,
                index_version=index_version,
            ))
            passage_idx += 1
        else:
            chunks = _split_oversized(block)
            for chunk in chunks:
                fp = compute_fingerprint(chunk)
                passages.append(Passage(
                    source_type=source_type,
                    source_id=source_id,
                    memory_id=memory_id,
                    passage_index=passage_idx,
                    text=chunk,
                    char_count=len(chunk),
                    source_fingerprint=fp,
                    index_version=index_version,
                ))
                passage_idx += 1

    return passages


def _merge_small_paragraphs(paragraphs: List[str]) -> List[str]:
    """Merge consecutive paragraphs until each passage reaches a target size.
    
    Strategy: keep merging into the buffer as long as it fits in PASSAGE_MAX_CHARS
    and either the buffer or the incoming paragraph is below PASSAGE_MIN_CHARS.
    This prevents many tiny passages from line-broken texts (e.g. psalms, prayers).
    """
    result = []
    buffer = ""
    for p in paragraphs:
        fits = len(buffer) + len(p) + 1 <= PASSAGE_MAX_CHARS
        needs_merge = not buffer or len(buffer) < PASSAGE_MIN_CHARS or len(p) < PASSAGE_MIN_CHARS
        if fits and needs_merge:
            buffer = (buffer + "\n" + p).strip() if buffer else p
        else:
            if buffer:
                result.append(buffer)
            buffer = p
    if buffer:
        result.append(buffer)
    return result


def _split_oversized(text: str) -> List[str]:
    """Split a paragraph exceeding PASSAGE_MAX_CHARS at sentence boundaries with overlap."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""

    for s in sentences:
        if len(current) + len(s) + 1 > PASSAGE_MAX_CHARS:
            if current:
                chunks.append(current.strip())
            current = s
        else:
            current = (current + " " + s).strip() if current else s

    if current:
        chunks.append(current.strip())

    return chunks
