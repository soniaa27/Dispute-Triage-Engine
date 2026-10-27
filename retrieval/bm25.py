"""Okapi BM25 keyword ranking. A few hundred chunks: plain Python, no extra dependency.

Catches what embeddings blur: exact terms such as "Table-I", "add-on card", "OTP" or a
rupee amount.
"""

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class BM25:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self._docs = [Counter(tokenize(d)) for d in documents]
        self._lengths = [sum(d.values()) for d in self._docs]
        self._avg_length = sum(self._lengths) / len(self._docs) if self._docs else 0.0
        df = Counter(term for doc in self._docs for term in doc)
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        """One score per document, in document order (0 if no query term occurs)."""
        terms = [t for t in set(tokenize(query)) if t in self._idf]
        scores = []
        for doc, length in zip(self._docs, self._lengths, strict=True):
            norm = self.k1 * (1 - self.b + self.b * length / self._avg_length)
            scores.append(sum(
                self._idf[t] * doc[t] * (self.k1 + 1) / (doc[t] + norm)
                for t in terms if t in doc
            ))
        return scores
