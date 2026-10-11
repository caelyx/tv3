"""Fuzzy title matching, match spans and matching-line snippets.

Pure functions on strings. A span is a half-open `(start, end)` range of
character indices; functions return spans sorted, non-overlapping and with
adjacent spans merged.

Smart case: a word that is all lowercase matches case-insensitively, any
other word matches case-sensitively, exactly as `brute_force_search` does.
Case-insensitive matching compares against `str.lower()`, which can turn one
character into two (U+0130 becomes "i" and a combining dot); indices found
in the lowercased text are mapped back to the original text.

Callers pass titles and contents in Unicode normalisation form NFC;
`fuzzy_needle` puts the query in NFC.
"""

import os
import re
import unicodedata
from collections.abc import Iterable, Sequence

import wcwidth
from pfzy import fzy_scorer

Span = tuple[int, int]

MIN_FUZZY_LENGTH = 3

SNIPPET_WIDTH = 80

ELLIPSIS = "…"

_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_LINE_BREAK = re.compile(f"[{re.escape(_LINE_BREAKS)}]")
_CONTROL_TO_SPACE = {c: " " for c in range(0xA0) if unicodedata.category(chr(c)) == "Cc"}


def printable(text: str) -> str:
    """`text` with each control character (Unicode category Cc) replaced by a space."""
    return text.translate(_CONTROL_TO_SPACE)


def fuzzy_needle(query: str) -> str | None:
    """The fuzzy-match needle for `query`: NFC-normalised, stripped and lowercased.

    None when the stripped query is shorter than MIN_FUZZY_LENGTH
    characters, counted before lowercasing. Inner spaces are kept and match
    spaces in titles.
    """
    stripped = unicodedata.normalize("NFC", query).strip()
    return stripped.lower() if len(stripped) >= MIN_FUZZY_LENGTH else None


def fuzzy_match(needle: str, title: str) -> tuple[float, tuple[Span, ...]] | None:
    """fzy score and matched spans of `needle` in `title`, or None if no match.

    `needle` must be lowercase (see `fuzzy_needle`) and matches `title`
    case-insensitively. Unless `needle` contains a path separator, it is
    matched against the title's last path component only, so folder names
    do not match; spans always index the whole title. Higher scores are
    better matches; a name equal to the needle scores infinity.
    """
    offset = 0 if os.sep in needle else title.rfind(os.sep) + 1
    name = title[offset:]
    lowered = name.lower()
    # pfzy lowercases the haystack itself and gives capitals after a
    # lowercase letter a bonus, so pass the original case when indices agree.
    same_length = len(lowered) == len(name)
    score, positions = fzy_scorer(needle, name if same_length else lowered)
    if score == float("-inf") or positions is None:
        return None
    if not same_length:
        positions = sorted({_floor_index(name, p) for p in positions})
    return score, _merge((offset + p, offset + p + 1) for p in positions)


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
    whitespace, cut around the word's first occurrence to at most `width`
    terminal columns (for a `width` of at least 2), as wcwidth measures
    them. ELLIPSIS marks each cut end and counts towards `width`. Cuts fall
    between grapheme clusters. Control characters (Unicode category Cc,
    such as tabs and C1 controls) become spaces. The spans cover every word
    of `words` in the excerpt, clipped to it.
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

    begin, end = _window(line, start - line_start, len(word), width)
    prefix = ELLIPSIS if begin > 0 else ""
    suffix = ELLIPSIS if end < len(line) else ""
    text = prefix + printable(line[begin:end]) + suffix

    margin = max(len(w) for w in words)
    context = max(0, begin - margin)
    shift = len(prefix) - begin
    spans = [
        (max(a + context, begin) + shift, min(b + context, end) + shift)
        for a, b in word_spans(line[context : end + margin], words)
        if a + context < end and b + context > begin
    ]
    return text, tuple(spans)


def _window(line: str, match: int, length: int, width: int) -> Span:
    """The range of `line` to show around the `length` characters at index
    `match`, cut between grapheme clusters.

    Leaves room for an ellipsis at each end that is cut, so the shown range
    plus its ellipses is at most `width` columns wide (for a `width` of at
    least 2). Up to a quarter of the width precedes the match, less if that
    would cut off a match that fits with both ellipses. If the line ends
    before the width is used up, the rest goes before the match too.
    """
    match_cols = wcwidth.width(printable(line[match : match + length]), control_codes="ignore")
    match = wcwidth.grapheme_boundary_before(line, match + 1)
    lead = width // 4
    if match_cols + 2 <= width:
        lead = min(lead, width - match_cols - 2)
    before = _cluster_sizes(wcwidth.iter_graphemes_reverse(line, 0, match), width)
    after = _cluster_sizes(wcwidth.iter_graphemes(line, match), width)
    whole_before = sum(n for n, _ in before) == match
    whole_after = sum(n for n, _ in after) == len(line) - match

    def is_cut(clusters, taken, whole):
        return taken < len(clusters) or not whole

    taken_before, cols_before = _fit(before, lead)
    cut_before = is_cut(before, taken_before, whole_before)
    room = width - cols_before - cut_before
    taken_after, cols_after = _fit(after, room)
    if is_cut(after, taken_after, whole_after):
        taken_after, cols_after = _fit(after, room - 1)
    elif cut_before:
        taken_before, _ = _fit(before, width - cols_after)
        if is_cut(before, taken_before, whole_before):
            taken_before, _ = _fit(before, width - cols_after - 1)
    begin = match - sum(n for n, _ in before[:taken_before])
    end = match + sum(n for n, _ in after[:taken_after])
    return begin, end


def _cluster_sizes(clusters: Iterable[str], width: int) -> list[tuple[int, int]]:
    """Length in characters and width in columns of each of `clusters`,
    until they are more than `width` columns wide in total."""
    sizes = []
    total = 0
    for cluster in clusters:
        if total > width:
            break
        cols = wcwidth.width(printable(cluster), control_codes="ignore")
        sizes.append((len(cluster), cols))
        total += cols
    return sizes


def _fit(sizes: Sequence[tuple[int, int]], width: int) -> tuple[int, int]:
    """How many of `sizes`, from the first, fit in `width` columns, and their width."""
    taken = total = 0
    for _, cols in sizes:
        if total + cols > width:
            break
        taken += 1
        total += cols
    return taken, total


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
