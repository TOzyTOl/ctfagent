"""Tokenisation shared by blackboard deduplication and the case library.

Both ask the same question — "do these two texts talk about the same thing?" —
and they must answer it the same way. Two implementations would drift, and a
case card that matches nothing is worse than no case card: it costs prompt
tokens on every dispatch.
"""

from __future__ import annotations

import re
import unicodedata

#: Words, numbers and addresses. CJK is written without spaces, so adjacent
#: character bigrams stand in for words in Chinese text.
WORD = re.compile(r"0x[0-9a-f]+|[0-9a-f]{8,}|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_.\-]{2,}")
#: List markers. NFKC turns "①" into "1", which would otherwise read as a new
#: value the earlier copy lacked; a re-numbered list is still the same list.
ENUM = re.compile(r"[①-⑳]|(?:(?<=\n)|(?<=[\s;；,，:：、]))\d{1,2}[.)、](?=\s|$)")
#: A value that carries information on its own: an address, a long hex string,
#: any number. A restatement that introduces one of these is not a restatement.
HARD = re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b|\d+")
#: An identifier-shaped token: the tool names, symbols and filenames a case card
#: is searched by (``gdb``, ``strace``, ``pyinstaller``, ``memcmp``).
IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}")

#: Words too common to say anything about a challenge.
STOPWORDS = frozenset(
    """
    the and for with that this from into your you are was were has have had not but its
    can could should would will may might must been being does did doing done than then
    there here what when where which while who whom whose how why all any both each few
    more most other some such only own same too very just also out off over under again
    further once about after before during above below between through against because
    until unless using used use uses get gets got make makes made see seen say says said
    one two three first second next last new old file files path dir directory work
    task tasks step steps result results note notes true false none null
    """.split()
)


def is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", ENUM.sub(" ", text or "")).lower()


def tokens(text: str) -> set[str]:
    """Words, numbers and CJK bigrams — the fuzzy matching vocabulary."""
    text = normalise(text)
    out = set(WORD.findall(text))
    out |= {
        text[i : i + 2]
        for i in range(len(text) - 1)
        if is_cjk(text[i]) and is_cjk(text[i + 1])
    }
    return out


def hard_tokens(text: str) -> set[str]:
    """Addresses and numbers: a restatement must not introduce a new one."""
    return set(HARD.findall(normalise(text)))


def identifiers(text: str) -> list[str]:
    """Identifier-shaped tokens, lowercased, stopwords removed, in first-seen order."""
    seen: dict[str, None] = {}
    for match in IDENT.finditer(text or ""):
        word = match.group(0).strip(".-_").lower()
        if len(word) < 3 or word in STOPWORDS or word.isdigit():
            continue
        seen.setdefault(word, None)
    return list(seen)
