"""Tests for ranking.py - fuzzy title matching, match spans and snippets."""

import random

import pytest
from pfzy import fzy_scorer

from tv3.ranking import fuzzy_match, fuzzy_needle, snippet, word_spans

DOTTED_I = "\u0130"  # U+0130 lowercases to two characters, "i" and a combining dot


class TestFuzzyNeedle:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("", None),
            ("   ", None),
            ("a", None),
            (" A ", None),
            ("ab", "ab"),
            ("  ZeB \t", "zeb"),
            ("a b", "a b"),
            ("Meeting  Notes", "meeting  notes"),
        ],
    )
    def test_strips_lowercases_and_needs_two_characters(self, query, expected):
        assert fuzzy_needle(query) == expected


class TestFuzzyMatch:
    @pytest.mark.parametrize(
        ("needle", "title", "spans"),
        [
            ("ab", "acb", ((0, 1), (2, 3))),
            ("ze", "zebra", ((0, 2),)),
            ("ze", "Zebra", ((0, 2),)),
            ("ze", "maze", ((2, 4),)),
            ("mtg", "Meeting", ((0, 1), (3, 4), (6, 7))),
            ("mn", "Meeting Notes", ((0, 1), (8, 9))),
            ("a b", "a xb", ((0, 2), (3, 4))),
            ("ab", "ab", ((0, 2),)),
            ("ab", "AB", ((0, 2),)),
            ("is", DOTTED_I + "stanbul", ((0, 2),)),
            ("st", DOTTED_I + "stanbul", ((1, 3),)),
            ("wp", "work/plan", ((0, 1), (5, 6))),
            ("cafe", "café", None),
            ("café", "Café crème", ((0, 4),)),
            ("ba", "ab", None),
            ("zz", "zebra", None),
            ("ab", "a", None),
            ("AB", "AXB", None),
        ],
    )
    def test_spans(self, needle, title, spans):
        match = fuzzy_match(needle, title)
        if spans is None:
            assert match is None
        else:
            assert match is not None
            assert match[1] == spans

    def test_agrees_with_pfzy_on_random_strings(self):
        rng = random.Random(0)
        alphabet = "abcAB .*?[(\\" + DOTTED_I + "\u0307é"
        for _ in range(5000):
            needle = "".join(rng.choice(alphabet) for _ in range(rng.randint(2, 4))).lower()
            title = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10)))
            score, _ = fzy_scorer(needle, title.lower())
            match = fuzzy_match(needle, title)
            if score == float("-inf"):
                assert match is None, (needle, title)
            else:
                assert match is not None, (needle, title)
                assert match[0] == score

    def test_non_subsequence_is_none_not_minus_infinity(self):
        assert fuzzy_match("xy", "abc") is None

    def test_uppercase_title_matches_lowercase_needle(self):
        # pfzy scores an uppercase needle against a lowercase title as
        # (-inf, [0, 0]); titles are lowercased so this never arises.
        assert fuzzy_match("ab", "AXB") == fuzzy_match("ab", "axb")

    @pytest.mark.parametrize(
        ("needle", "better", "worse"),
        [
            ("mtg", "mtg plan", "meeting"),
            ("ze", "zebra", "maze"),
            ("ab", "ab", "abc"),
            ("abc", "abc", "a b c"),
            ("np", "new plan", "unpinned"),
        ],
    )
    def test_score_order(self, needle, better, worse):
        better_match = fuzzy_match(needle, better)
        worse_match = fuzzy_match(needle, worse)
        assert better_match is not None
        assert worse_match is not None
        assert better_match[0] > worse_match[0]

    def test_equal_titles_score_equal(self):
        first = fuzzy_match("abc", "abc 1")
        second = fuzzy_match("abc", "abc 2")
        assert first is not None
        assert second is not None
        assert first[0] == second[0]


class TestWordSpans:
    @pytest.mark.parametrize(
        ("text", "words", "spans"),
        [
            ("Meeting notes", [], ()),
            ("Meeting notes", ["meeting"], ((0, 7),)),
            ("Meeting notes", ["Meeting"], ((0, 7),)),
            ("Meeting notes", ["MEETING"], ()),
            ("Meeting notes", ["meet", "notes"], ((0, 4), (8, 13))),
            ("Meeting notes", ["notes", "meet"], ((0, 4), (8, 13))),
            ("Meeting notes", ["xyz"], ()),
            ("abcabc", ["bc"], ((1, 3), (4, 6))),
            ("aaa", ["aa"], ((0, 3),)),
            ("abc", ["ab", "bc"], ((0, 3),)),
            ("abc", ["ab", "c"], ((0, 3),)),
            ("foo foo", ["foo", "foo"], ((0, 3), (4, 7))),
            ("Café CAFÉ café", ["café"], ((0, 4), (5, 9), (10, 14))),
            ("Café CAFÉ café", ["CAFÉ"], ((5, 9),)),
            (DOTTED_I + "stanbul", ["stan"], ((1, 5),)),
            (DOTTED_I + "stanbul", ["i"], ((0, 1),)),
            ("x" + DOTTED_I + "y", ["xi\u0307y"], ((0, 3),)),
            ("x" + DOTTED_I + "y", ["\u0307y"], ((1, 3),)),
            ("ΟΔΟΣ", ["οδο"], ((0, 3),)),
            ("🙂 smile 🙂", ["🙂"], ((0, 1), (8, 9))),
        ],
    )
    def test_spans(self, text, words, spans):
        assert word_spans(text, words) == spans


class TestSnippet:
    @pytest.mark.parametrize(
        ("title", "contents", "words", "expected"),
        [
            ("plan", "", ["budget"], None),
            ("plan", "nothing here", [], None),
            ("budget plan", "the budget is", ["budget"], None),
            ("budget plan", "the budget is", ["plan", "budget"], None),
            ("Budget plan", "the budget is", ["budget"], None),
            ("Budget plan", "the budget is", ["BUDGET"], None),
            ("plan", "first\nthe budget is\nlast", ["budget"], ("the budget is", ((4, 10),))),
            (
                "plan",
                "first\nthe budget is\nlast budget",
                ["plan", "budget"],
                ("the budget is", ((4, 10),)),
            ),
            ("plan", "no Budget\nyes budget", ["budget"], ("no Budget", ((3, 9),))),
            ("plan", "no Budget\nyes budget", ["Budget"], ("no Budget", ((3, 9),))),
            ("plan", "BUDGET\nyes budget", ["Budget"], None),
            (
                "plan",
                "eggs milk\nmilk and eggs",
                ["milk", "eggs"],
                ("eggs milk", ((0, 4), (5, 9))),
            ),
            ("plan", "abcabc", ["bc", "ab"], ("abcabc", ((0, 6),))),
            ("plan", "line one\r\nthe budget\r\n", ["budget"], ("the budget", ((4, 10),))),
            ("plan", "a\x0bbudget\x85b", ["budget"], ("budget", ((0, 6),))),
            ("plan", "\tthe\tbudget\t", ["budget"], ("the budget", ((4, 10),))),
            ("plan", "   indented budget   ", ["budget"], ("indented budget", ((9, 15),))),
            ("plan", "esc\x1b[31mbudget", ["budget"], ("esc [31mbudget", ((8, 14),))),
            ("plan", "x" + DOTTED_I + "budget", ["budget"], ("x" + DOTTED_I + "budget", ((2, 8),))),
            ("plan", DOTTED_I + "i budget", ["i"], (DOTTED_I + "i budget", ((0, 2),))),
        ],
    )
    def test_line_and_spans(self, title, contents, words, expected):
        assert snippet(title, contents, words, width=40) == expected

    @pytest.mark.parametrize(
        ("line", "word", "width", "expected"),
        [
            (
                "budget is at the start of a long line",
                "budget",
                20,
                ("budget is at the st…", ((0, 6),)),
            ),
            (
                "a long line that ends with the budget",
                "budget",
                20,
                ("…nds with the budget", ((14, 20),)),
            ),
            ("0123456789 budget 0123456789", "budget", 20, ("…6789 budget 012345…", ((6, 12),))),
            ("exactly twenty chars", "twenty", 20, ("exactly twenty chars", ((8, 14),))),
            ("twenty-one characters", "one", 20, ("…enty-one characters", ((6, 9),))),
            ("x" * 50 + "budget" + "y" * 50, "budget", 10, ("…xxbudget…", ((3, 9),))),
            (
                "x" * 50 + "abcdefghijklmnop" + "y" * 50,
                "abcdefghijklmnop",
                10,
                ("…xxabcdef…", ((3, 9),)),
            ),
        ],
    )
    def test_window(self, line, word, width, expected):
        assert snippet("title", line, [word], width=width) == expected

    def test_window_spans_include_every_word_and_clip_at_edges(self):
        line = "alpha beta " + "pad " * 10 + "budget " + "pad " * 10 + "alphabet"
        text, spans = snippet("title", line, ["budget", "alpha", "pad"], width=30)

        assert len(text) == 30
        assert text.startswith("…") and text.endswith("…")
        assert "budget" in text
        highlighted = [text[a:b] for a, b in spans]
        assert "budget" in highlighted
        assert all(set(h.replace(" ", "")) <= set("budgetpad") for h in highlighted)

    @pytest.mark.parametrize(
        ("line", "words", "width", "expected"),
        [
            ("zzzzabcdefbudget" + "y" * 20, ["budget", "abcdef"], 10, ("…efbudget…", ((1, 9),))),
            (
                "y" * 20 + "budget" + "ab" + "z" * 20,
                ["budget", "ab"],
                12,
                ("…yyybudgeta…", ((4, 11),)),
            ),
        ],
    )
    def test_words_cut_by_the_window_are_clipped(self, line, words, width, expected):
        assert snippet("title", line, words, width=width) == expected

    def test_very_long_line(self):
        line = "word " * 200_000 + "needle" + " tail" * 200_000
        text, spans = snippet("title", "first\n" + line, ["needle"], width=30)

        assert len(text) == 30
        assert [text[a:b] for a, b in spans] == ["needle"]

    def test_very_long_line_with_dotted_capital_i(self):
        line = (DOTTED_I + " ") * 100_000 + "Needle"
        text, spans = snippet("title", line, ["needle"], width=12)

        assert text == "… " + DOTTED_I + " " + DOTTED_I + " Needle"
        assert [text[a:b] for a, b in spans] == ["Needle"]
