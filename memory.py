"""
memory.py — Pure-Python keyword search over MEMORY.md + memory/*.md files.
Uses BM25-style scoring with no external dependencies (replaces sklearn TF-IDF).
"""

import glob
import math
import os
import re
import time
from collections import Counter
from pathlib import Path

from config import MEMORY_DIR, MEMORY_FILE, WORKSPACE

INDEX_TTL = 300  # Rebuild every 5 min

_chunks: list[dict] = []
_index_time: float = 0
_file_mtimes: dict[str, float] = {}
# BM25 index data
_doc_freqs: dict[str, int] = {}   # term -> number of chunks containing it
_doc_lens: list[int] = []          # token count per chunk
_doc_terms: list[Counter] = []     # term frequencies per chunk
_avg_dl: float = 0.0
_N: int = 0

# BM25 tuning
_K1 = 1.5
_B = 0.75

_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "this", "that", "are", "was",
    "be", "have", "has", "had", "not", "no", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "if", "then", "so",
    "as", "up", "out", "about", "into", "over", "after", "before", "between",
    "each", "all", "both", "few", "more", "most", "other", "some", "such",
    "than", "too", "very", "just", "also", "now", "here", "there", "when",
    "where", "how", "what", "which", "who", "whom", "its", "my", "your",
    "his", "her", "our", "their", "we", "you", "he", "she", "they", "i",
    "me", "him", "us", "them", "been", "being", "were", "am",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, drop stop words and short tokens."""
    tokens = re.findall(r'[a-z0-9_]+', text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


def _needs_reindex() -> bool:
    if not _chunks or time.time() - _index_time > INDEX_TTL:
        return True
    for f in _get_files():
        try:
            if _file_mtimes.get(f) != os.path.getmtime(f):
                return True
        except OSError:
            pass
    return False


def _get_files() -> list[str]:
    files = []
    mf = str(MEMORY_FILE)
    if os.path.isfile(mf):
        files.append(mf)
    md = str(MEMORY_DIR)
    if os.path.isdir(md):
        files.extend(sorted(glob.glob(os.path.join(md, "*.md"))))
    return files


def _chunk_file(filepath: str) -> list[dict]:
    try:
        content = Path(filepath).read_text(errors="replace")
    except Exception:
        return []
    lines = content.split("\n")
    chunks = []
    header = os.path.basename(filepath)
    current_lines = []
    start = 1
    for i, line in enumerate(lines, 1):
        if re.match(r'^#{1,3}\s', line):
            if current_lines:
                text = "\n".join(current_lines).strip()
                if text and len(text) > 20:
                    chunks.append({"path": filepath, "header": header, "text": text, "line_start": start, "line_end": i - 1})
            header = line.strip().lstrip("#").strip()
            current_lines = [line]
            start = i
        else:
            current_lines.append(line)
    if current_lines:
        text = "\n".join(current_lines).strip()
        if text and len(text) > 20:
            chunks.append({"path": filepath, "header": header, "text": text, "line_start": start, "line_end": len(lines)})
    return chunks


def _build_index():
    global _chunks, _index_time, _file_mtimes
    global _doc_freqs, _doc_lens, _doc_terms, _avg_dl, _N

    files = _get_files()
    all_chunks = []
    mtimes = {}
    for f in files:
        all_chunks.extend(_chunk_file(f))
        try:
            mtimes[f] = os.path.getmtime(f)
        except OSError:
            pass

    if not all_chunks:
        _chunks = []
        _doc_freqs, _doc_lens, _doc_terms = {}, [], []
        _avg_dl, _N = 0.0, 0
        _index_time = time.time()
        _file_mtimes = mtimes
        return

    # Build BM25 index
    doc_terms = []
    doc_lens = []
    doc_freqs: dict[str, int] = {}

    for chunk in all_chunks:
        tokens = _tokenize(chunk["text"])
        tf = Counter(tokens)
        doc_terms.append(tf)
        doc_lens.append(len(tokens))
        for term in tf:
            doc_freqs[term] = doc_freqs.get(term, 0) + 1

    _chunks = all_chunks
    _doc_terms = doc_terms
    _doc_lens = doc_lens
    _doc_freqs = doc_freqs
    _N = len(all_chunks)
    _avg_dl = sum(doc_lens) / _N if _N else 1.0
    _index_time = time.time()
    _file_mtimes = mtimes


def search(query: str, max_results: int = 5, min_score: float = 0.1) -> list[dict]:
    if _needs_reindex():
        _build_index()
    if not _chunks or _N == 0:
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    scores = []
    for i in range(_N):
        score = 0.0
        dl = _doc_lens[i]
        tf_map = _doc_terms[i]
        for term in query_tokens:
            if term not in tf_map:
                continue
            tf = tf_map[term]
            df = _doc_freqs.get(term, 0)
            # BM25 IDF: log((N - df + 0.5) / (df + 0.5) + 1)
            idf = math.log(((_N - df + 0.5) / (df + 0.5)) + 1.0)
            # BM25 TF normalization
            tf_norm = (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * dl / _avg_dl))
            score += idf * tf_norm
        scores.append(score)

    # Sort by score descending
    ranked = sorted(range(_N), key=lambda i: scores[i], reverse=True)

    results = []
    for idx in ranked:
        s = scores[idx]
        if s < min_score or len(results) >= max_results:
            break
        chunk = _chunks[idx]
        snippet = chunk["text"][:500] + ("..." if len(chunk["text"]) > 500 else "")
        results.append({
            "path": chunk["path"],
            "header": chunk["header"],
            "snippet": snippet,
            "line_start": chunk["line_start"],
            "line_end": chunk["line_end"],
            "score": round(s, 4),
        })
    return results


def read_memory(path: str, from_line: int = 1, max_lines: int = 100) -> str:
    resolved = os.path.realpath(path)
    ws_real = os.path.realpath(str(WORKSPACE))
    if not resolved.startswith(ws_real):
        return f"ERROR: Path must be within workspace ({WORKSPACE})"
    if not os.path.isfile(resolved):
        return f"ERROR: File not found: {path}"
    try:
        lines = Path(resolved).read_text(errors="replace").split("\n")
        s = max(0, from_line - 1)
        return "\n".join(lines[s:s + max_lines])
    except Exception as e:
        return f"ERROR: {e}"
