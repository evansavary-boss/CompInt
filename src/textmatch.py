"""Fast multi-term matching with offsets aligned on the original text.

`fold()` lowercases and strips accents character by character, so a match found in the
folded text has the same offsets in the original text. This lets case-insensitive and
case-sensitive terms be compared span by span (longest match wins).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

_PLURAL_SUFFIX = r"(?:s|x|es)?"
_SPECIAL = {"’": "'", "‘": "'", "²": "2", " ": " "}


def fold(text: str) -> str:
    """Lowercase and strip accents, keeping exactly one output char per input char."""
    out = []
    for ch in text or "":
        if ch in _SPECIAL:
            out.append(_SPECIAL[ch])
            continue
        base = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in base if not unicodedata.combining(c)) or ch
        out.append(base.lower()[0])
    return "".join(out)


def _canon(term: str) -> str:
    return re.sub(r"[\s-]+", " ", term).strip()


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    target: str
    term: str

    @property
    def length(self) -> int:
        return self.end - self.start


class TermIndex:
    """Map many terms to targets (entity ids, country codes, event keys...).

    Case-insensitive terms match on folded text and accept simple plurals when
    `plurals` is True; case-sensitive terms (acronyms) match the original text exactly.
    """

    def __init__(self, plurals: bool = False):
        self.plurals = plurals
        self._ci: dict[str, set[str]] = {}
        self._cs: dict[str, set[str]] = {}
        self._rx_ci: re.Pattern | None = None
        self._rx_cs: re.Pattern | None = None

    def add(self, term: str, target: str, case_sensitive: bool = False) -> None:
        term = (term or "").strip()
        if not term:
            return
        table = self._cs if case_sensitive else self._ci
        key = _canon(term) if case_sensitive else _canon(fold(term))
        table.setdefault(key, set()).add(target)
        self._rx_ci = self._rx_cs = None

    def add_many(self, terms: Iterable[str], target: str, case_sensitive: bool = False) -> None:
        for t in terms or []:
            self.add(t, target, case_sensitive)

    @staticmethod
    def _compile(keys: Iterable[str], plurals: bool) -> re.Pattern | None:
        parts = []
        for key in sorted(keys, key=len, reverse=True):
            tokens = [re.escape(t) for t in key.split(" ")]
            parts.append(r"[\s-]+".join(tokens) + (_PLURAL_SUFFIX if plurals else ""))
        if not parts:
            return None
        return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)")

    def _lookup(self, table: dict[str, set[str]], matched: str) -> tuple[str, set[str]] | None:
        key = _canon(matched)
        if key in table:
            return key, table[key]
        if self.plurals:
            for suffix in ("es", "s", "x"):
                if key.endswith(suffix) and key[: -len(suffix)] in table:
                    return key[: -len(suffix)], table[key[: -len(suffix)]]
        return None

    def find(self, text: str) -> list[Span]:
        """All matches, overlapping ones resolved in favour of the longest span."""
        if self._rx_ci is None and self._ci:
            self._rx_ci = self._compile(self._ci, self.plurals)
        if self._rx_cs is None and self._cs:
            self._rx_cs = self._compile(self._cs, plurals=False)

        spans: list[Span] = []
        for rx, haystack, table in ((self._rx_ci, fold(text), self._ci), (self._rx_cs, text or "", self._cs)):
            if rx is None:
                continue
            for m in rx.finditer(haystack):
                hit = self._lookup(table, m.group(0))
                if hit:
                    for target in sorted(hit[1]):
                        spans.append(Span(m.start(), m.end(), target, hit[0]))

        kept: list[Span] = []
        for span in sorted(spans, key=lambda s: (-s.length, s.start)):
            if not any(span.start < k.end and k.start < span.end and k.target != span.target
                       and k.length > span.length for k in kept):
                kept.append(span)
        return sorted(kept, key=lambda s: s.start)

    def targets(self, text: str) -> list[str]:
        """Distinct targets in order of first appearance."""
        seen: list[str] = []
        for span in self.find(text):
            if span.target not in seen:
                seen.append(span.target)
        return seen
