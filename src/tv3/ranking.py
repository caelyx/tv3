"""Fuzzy title matching, match spans and matching-line snippets.

Pure functions on strings. A span is a half-open `(start, end)` range of
character indices; functions return spans sorted, non-overlapping and with
adjacent spans merged.

Smart case: a word that is all lowercase matches case-insensitively, any
other word matches case-sensitively, exactly as `brute_force_search` does.
Case-insensitive matching compares against `str.lower()`, which can turn one
character into two (U+0130 becomes "i" and a combining dot); indices found
in the lowercased text are mapped back to the original text.
"""

import functools
import re
from collections.abc import Iterable, Sequence

from pfzy import fzy_scorer

Span = tuple[int, int]

MIN_FUZZY_LENGTH = 2

SNIPPET_WIDTH = 80

ELLIPSIS = "…"

_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_LINE_BREAK = re.compile(f"[{re.escape(_LINE_BREAKS)}]")
_CONTROL_TO_SPACE = dict.fromkeys([*range(0x20), 0x7F], " ")


def fuzzy_needle(query: str) -> str | None:
    """The fuzzy-match needle for `query`: stripped and lowercased.

    None when it is shorter than MIN_FUZZY_LENGTH characters. Inner spaces
    are kept and match spaces in titles.
    """
    needle = query.strip().lower()
    return needle if len(needle) >= MIN_FUZZY_LENGTH else None


def fuzzy_match(needle: str, title: str) -> tuple[float, tuple[Span, ...]] | None:
    """fzy score and matched spans of `needle` in `title`, or None if no match.

    `needle` must be lowercase (see `fuzzy_needle`); `title` is lowercased
    here. Higher scores are better matches; a title equal to the needle
    scores infinity.
    """
    lowered = title.lower()
    if not _subsequence_pattern(needle).search(lowered):
        return None
    score, positions = fzy_scorer(needle, lowered)
    if score == float("-inf") or positions is None:
        return None
    if len(lowered) != len(title):
        positions = sorted({_floor_index(title, p) for p in positions})
    return score, _merge((p, p + 1) for p in positions)


@functools.lru_cache(maxsize=8)
def _subsequence_pattern(needle: str) -> re.Pattern[str]:
    """A regex that finds `needle`'s characters in order, the test fzy_scorer starts with."""
    return re.compile(".*?".join(map(re.escape, needle)), re.DOTALL)


def word_spans(text: str, words: Sequence[str]) -> tuple[Span, ...]:
    """Spans of every occurrence of every word of `words` in `text`, with smart case."""
    spans: list[Span] = []
    lowered: str | None = None
    for word in words:
        if word.islower():
            if lowered is None:
                lowered = text.lower()
            spans.extend(_occurrences(text, lowered, word))
        else:
            spans.extend(_occurrences(text, text, word))
    return _merge(spans)


def contains(text: str, word: str) -> bool:
    """Whether `word` occurs in `text`, with smart case."""
    return word in (text.lower() if word.islower() else text)


def snippet(
    title: str, contents: str, words: Sequence[str], width: int = SNIPPET_WIDTH
) -> tuple[str, tuple[Span, ...]] | None:
    """A one-line excerpt of `contents` showing why a note matched, and its spans.

    None if `title` contains every word or `contents` does not contain the
    first word missing from the title. Otherwise the excerpt is the first
    line of `contents` containing that word, without leading and trailing
    whitespace, cut to at most `width` characters around the word's first
    occurrence. ELLIPSIS marks each cut end and counts towards `width`.
    Control characters such as tabs become spaces. The spans cover every
    word of `words` in the excerpt, clipped to it.
    """
    word = next((w for w in words if not contains(title, w)), None)
    if word is None:
        return None
    start = _find(contents, word)
    if start < 0:
        return None
    line_start = max(contents.rfind(c, 0, start) for c in _LINE_BREAKS) + 1
    line_break = _LINE_BREAK.search(contents, start)
    line_end = line_break.start() if line_break else len(contents)
    line = contents[line_start:line_end]
    stripped = line.lstrip()
    line_start += len(line) - len(stripped)
    line = stripped.rstrip()

    begin, end = _window(len(line), start - line_start, width)
    prefix = ELLIPSIS if begin > 0 else ""
    suffix = ELLIPSIS if end < len(line) else ""
    text = prefix + line[begin:end].translate(_CONTROL_TO_SPACE) + suffix

    margin = max(len(w) for w in words)
    context = max(0, begin - margin)
    shift = len(prefix) - begin
    spans = [
        (max(a + context, begin) + shift, min(b + context, end) + shift)
        for a, b in word_spans(line[context : end + margin], words)
        if a + context < end and b + context > begin
    ]
    return text, tuple(spans)


def _window(length: int, match: int, width: int) -> Span:
    """The range of a line of `length` characters to show around index `match`.

    Leaves room for an ellipsis at each end that is cut, so the shown range
    plus its ellipses is at most `width` characters. About a quarter of the
    width precedes the match.
    """
    if length <= width:
        return 0, length
    begin = max(0, match - width // 4)
    if begin == 0:
        return 0, width - 1
    end = begin + width - 1
    if end >= length:
        return length - (width - 1), length
    return begin, end - 1


def _find(text: str, word: str) -> int:
    """Index in `text` of the first occurrence of `word`, with smart case, or -1."""
    if not word.islower():
        return text.find(word)
    lowered = text.lower()
    index = lowered.find(word)
    if index < 0 or len(lowered) == len(text):
        return index
    return _floor_index(text, index)


def _occurrences(text: str, haystack: str, word: str) -> Iterable[Span]:
    """Spans in `text` of each occurrence of `word` in `haystack`.

    `haystack` is `text` or `text.lower()`. Occurrences may overlap.
    """
    mapped = len(haystack) != len(text)
    index = haystack.find(word)
    while index >= 0:
        end = index + len(word)
        if mapped:
            yield _floor_index(text, index), _ceil_index(text, end)
        else:
            yield index, end
        index = haystack.find(word, index + 1)


def _floor_index(text: str, lowered_index: int) -> int:
    """Index of the character of `text` whose lowercase form holds `lowered_index`.

    The largest i with len(text[:i].lower()) <= lowered_index.
    """
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].lower()) <= lowered_index:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _ceil_index(text: str, lowered_end: int) -> int:
    """The smallest i with len(text[:i].lower()) >= lowered_end."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if len(text[:mid].lower()) >= lowered_end:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _merge(spans: Iterable[Span]) -> tuple[Span, ...]:
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return tuple(merged)
