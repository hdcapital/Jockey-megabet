"""Shared name-normalisation utilities for jockeys, runners and meetings.

The same real-world entity is spelled differently across feeds:

* ``James McDonald`` vs ``J McDonald`` vs ``JAMES MCDONALD (a)``
* ``1. Winx (NZ)`` vs ``WINX``
* ``O'Brien`` vs ``OBrien`` vs ``O`Brien``

Normalisation is deliberately conservative: it strips presentation noise
(case, punctuation, saddlecloth prefixes, bracketed suffixes, apprentice
claims) but never guesses between two genuinely different names. Ambiguous
matches are reported as unmatched rather than silently resolved.
"""

from __future__ import annotations

import re
import unicodedata

_BRACKET_SUFFIX = re.compile(r"\s*\(([^)]*)\)\s*$")
_LEADING_NUMBER = re.compile(r"^\s*\d+\s*[.)-]?\s*")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})
_NON_NAME = re.compile(r"[^a-z0-9' ]+")
_WS = re.compile(r"\s+")


def _ascii_fold(text: str) -> str:
    return (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )


def strip_bracketed_suffixes(name: str) -> str:
    """Remove trailing bracketed qualifiers, repeatedly.

    Handles country/state tags on horses (``(NZ)``, ``(GB)``, ``(IRE)``)
    and apprentice/claim tags on jockeys (``(a)``, ``(a3)``, ``(2kg)``).
    """
    prev = None
    while prev != name:
        prev = name
        name = _BRACKET_SUFFIX.sub("", name)
    return name


def normalize_name(name: str) -> str:
    """Canonical lowercase form for comparing names across feeds."""
    s = name.translate(_APOSTROPHES)
    s = _ascii_fold(s)
    s = _LEADING_NUMBER.sub("", s)
    s = strip_bracketed_suffixes(s)
    s = s.lower()
    s = _NON_NAME.sub(" ", s)
    s = s.replace("'", "")  # O'Brien == OBrien
    s = _WS.sub(" ", s).strip()
    return s


def name_tokens(name: str) -> list[str]:
    return normalize_name(name).split()


def surname_and_initial(name: str) -> tuple[str, str] | None:
    """(surname, first initial) — the invariant most feeds preserve.

    ``James McDonald`` -> ("mcdonald", "j"); ``J McDonald`` -> ("mcdonald", "j").
    Returns None when there aren't at least two tokens.
    """
    tokens = name_tokens(name)
    if len(tokens) < 2:
        return None
    return tokens[-1], tokens[0][0]


def jockey_names_match(a: str, b: str) -> bool:
    """Match two jockey name spellings without guessing.

    Exact normalized match, or same surname + same first initial where one
    side abbreviates the first name. ``James McDonald`` vs ``J McDonald``
    matches; ``J Smith`` vs ``James Smyth`` does not.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = surname_and_initial(a), surname_and_initial(b)
    if sa is None or sb is None:
        return False
    if sa[0] != sb[0] or sa[1] != sb[1]:
        return False
    # Same surname + initial: accept only if one first name is an
    # abbreviation (initial) of the other, not two different full names.
    fa, fb = name_tokens(a)[0], name_tokens(b)[0]
    if fa == fb:
        return True
    return len(fa) == 1 or len(fb) == 1 or fa.startswith(fb) or fb.startswith(fa)


def runner_names_match(a: str, b: str) -> bool:
    """Match two horse-name spellings (numbers/suffixes stripped, exact core)."""
    na, nb = normalize_name(a), normalize_name(b)
    return bool(na) and na == nb


def venue_names_match(a: str, b: str) -> bool:
    """Match meeting/venue names ('Royal Randwick' vs 'Randwick' allowed)."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    # One name's tokens fully contained in the other's (e.g. royal randwick).
    return ta <= tb or tb <= ta
