"""Property-based tests for ``utils.normalize_model_name``.

**Validates: Requirements 3.2**

``normalize_model_name`` is the dedup-key normaliser used by the composite
key ``(brand.lower(), normalize_model_name(model_name), canonical_path(url))``
(design section 3). It strips, collapses internal whitespace runs to a
single space, and lower-cases the result. The properties below exercise
the two universal invariants this implies:

  * **Idempotence** — applying the normaliser twice equals applying it
    once. Any subsequent re-normalisation of an already-normalised key
    must be a no-op or dedup keys would silently drift between callers.
  * **Whitespace stability** — the normalised form depends only on the
    lower-cased non-whitespace tokens, not on *which* or *how many*
    whitespace characters separate them. Inserting any ``\\s+`` run
    between (or around) tokens must not change the output.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from utils import normalize_model_name


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Arbitrary text excluding surrogate code points (Python's ``re`` and
# ``str.lower()`` reject those). This lets idempotence be tested against
# the full practical Unicode space, including exotic whitespace, case
# folding, and non-printable characters.
arbitrary_text_st = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=80,
)

# Tokens composed of ASCII alphanumerics — guaranteed to contain no
# whitespace, so re-joining them with arbitrary whitespace runs cannot
# change which tokens emerge after normalisation.
word_st = (
    st.from_regex(r"[a-zA-Z0-9]+", fullmatch=True)
    .filter(lambda w: 1 <= len(w) <= 10)
)

# Whitespace separators: one or more ASCII whitespace characters drawn
# from the set ``re``'s ``\\s`` and ``str.split()`` both classify as
# whitespace (so the boundaries they detect always agree).
whitespace_run_st = (
    st.from_regex(r"[ \t\n\r\f\v]+", fullmatch=True)
    .filter(lambda w: 1 <= len(w) <= 5)
)


@st.composite
def words_with_whitespace_padding(draw):
    """Draw ``(words, joined)`` where ``joined`` interleaves ``words`` with
    arbitrary whitespace runs and may carry leading / trailing whitespace.

    ``words`` may be empty, in which case ``joined`` is purely whitespace
    (or the empty string) — both of which must normalise to ``""``.
    """
    words = draw(st.lists(word_st, min_size=0, max_size=8))
    leading = draw(st.one_of(st.just(""), whitespace_run_st))
    trailing = draw(st.one_of(st.just(""), whitespace_run_st))

    if not words:
        return [], leading + trailing

    n_seps = len(words) - 1
    seps = draw(st.lists(whitespace_run_st, min_size=n_seps, max_size=n_seps))

    parts: list[str] = [leading, words[0]]
    for sep, word in zip(seps, words[1:]):
        parts.append(sep)
        parts.append(word)
    parts.append(trailing)
    return words, "".join(parts)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(s=arbitrary_text_st)
@settings(max_examples=200, deadline=None)
def test_normalize_model_name_is_idempotent(s):
    """Property 4a: ``normalize_model_name`` is idempotent.

    For every string ``s``::

        normalize_model_name(normalize_model_name(s)) == normalize_model_name(s)

    Once a name has been collapsed and lower-cased, re-running the
    normaliser must be a fixed point — otherwise dedup keys could differ
    between consumers that normalise once and consumers that normalise
    twice.

    **Validates: Requirements 3.2**
    """
    once = normalize_model_name(s)
    twice = normalize_model_name(once)
    assert twice == once


@given(data=words_with_whitespace_padding())
@settings(max_examples=200, deadline=None)
def test_normalize_model_name_is_whitespace_stable(data):
    """Property 4b: the normaliser is stable under whitespace injection.

    For any list of alphanumeric tokens ``words``, joining them with
    arbitrary ASCII whitespace runs (and any leading / trailing whitespace)
    must produce the same normalised string as joining them with a single
    space and lower-casing each token::

        normalize_model_name(joined) == " ".join(w.lower() for w in words)

    This is the universal "inserting any ``\\s+`` runs into ``s`` does
    not change the output" property called out by task 2.4.

    **Validates: Requirements 3.2**
    """
    words, joined = data
    expected = " ".join(w.lower() for w in words)
    assert normalize_model_name(joined) == expected
