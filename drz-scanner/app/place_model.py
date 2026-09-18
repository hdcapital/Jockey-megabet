"""Discounted Harville place probabilities (Lo / Bacon-Shone form).

Plain Harville assumes the finishing order is a sequence of independent
draws proportional to win probability. That is known to be wrong in the same
direction every time: it over-states the chance that a strong favourite runs
a place. The Lo-Bacon-Shone correction raises the win probabilities to a
power below 1 at each subsequent finishing position, flattening the field as
the race resolves.

With ``q`` the normalised win probabilities and two exponents ``lam`` and
``tau``::

    a = q ** lam           b = q ** tau
    A = sum(a)             B = sum(b)

    P(j finishes 1st)                 = q_j
    P(i 2nd | j 1st)                  = a_i / (A - a_j)
    P(k 3rd | j 1st, i 2nd)           = b_k / (B - b_j - b_i)

``lam = tau = 1`` recovers plain Harville exactly.

The exponents are **not** hard-coded: they are loaded from
``data/calibration.json`` and refitted by ``python -m app.calibrate``. The
shipped values (lam 0.71, tau 0.70) were fitted by maximum likelihood on
44,856 Australian thoroughbred races — see the README for the measurements.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "PlaceProbabilities",
    "place_probabilities",
    "place_probabilities_batch",
    "places_for_field",
    "PLACE_TERMS_FRAGILE_FIELD",
]

# Sportsbet pays three place dividends from 8 active runners, two from 5-7,
# and offers no place market below 5. A field of exactly 8 is one scratching
# away from paying only two dividends, which is why it is flagged.
PLACE_TERMS_FRAGILE_FIELD = 8


def places_for_field(n_active: int) -> int:
    """Number of place dividends for a field of ``n_active`` runners.

    Returns 0 when the field is too small for a place market at all.
    """
    if n_active >= 8:
        return 3
    if n_active >= 5:
        return 2
    return 0


@dataclass(frozen=True)
class PlaceProbabilities:
    """Per-runner place probabilities plus the pieces they were built from."""

    p_place: tuple[float, ...]
    p_first: tuple[float, ...]
    p_second: tuple[float, ...]
    p_third: tuple[float, ...]
    places: int
    lam: float
    tau: float

    def check_sum(self, tol: float = 1e-9) -> None:
        total = sum(self.p_place)
        if abs(total - self.places) > tol:
            raise AssertionError(
                f"place probabilities sum to {total!r}, expected {self.places}"
            )


def _validate(q: np.ndarray) -> None:
    if q.ndim != 2:
        raise ValueError("q must be 2-D (races x runners)")
    if np.any(q < 0):
        raise ValueError("win probabilities must be non-negative")


def place_probabilities_batch(
    q: np.ndarray,
    mask: np.ndarray,
    places: np.ndarray,
    lam: float,
    tau: float,
) -> np.ndarray:
    """Vectorised discounted Harville over a padded batch of races.

    Parameters
    ----------
    q
        ``(R, N)`` win probabilities, each *active* row normalised to 1.
        Padding slots must be 0.
    mask
        ``(R, N)`` boolean; True where the slot is a real active runner.
    places
        ``(R,)`` number of place dividends per race (2 or 3).

    Returns ``(R, N)`` place probabilities, 0 in padding slots. Each row sums
    to its race's number of places.
    """
    q = np.asarray(q, dtype=float)
    mask = np.asarray(mask, dtype=bool)
    places = np.asarray(places)
    _validate(q)

    a = np.where(mask, np.power(q, lam), 0.0)
    b = np.where(mask, np.power(q, tau), 0.0)
    A = a.sum(axis=1)[:, None]
    B = b.sum(axis=1)[:, None]

    # M[r, j, i] = P(j 1st) * P(i 2nd | j 1st)
    denom_a = A[:, :, None] - a[:, :, None]
    M = q[:, :, None] * np.divide(
        a[:, None, :], denom_a, out=np.zeros_like(denom_a * a[:, None, :]),
        where=denom_a > 0,
    )
    idx = np.arange(q.shape[1])
    M[:, idx, idx] = 0.0
    second = M.sum(axis=1)

    # T[r, j, i] = M[r, j, i] / (B - b_j - b_i); the third-place numerator
    # b_k is factored out so one tensor serves every k.
    denom_b = B[:, :, None] - b[:, :, None] - b[:, None, :]
    T = np.divide(M, denom_b, out=np.zeros_like(M), where=denom_b > 0)
    T[:, idx, idx] = 0.0
    total = T.sum(axis=(1, 2))[:, None]
    third = b * (total - T.sum(axis=2) - T.sum(axis=1))

    out = q + second + np.where(places[:, None] == 3, third, 0.0)
    return np.where(mask, out, 0.0)


def place_probabilities(
    win_probs, places: int, lam: float, tau: float
) -> PlaceProbabilities:
    """Discounted Harville for a single race.

    ``win_probs`` must be the win probabilities of the *active* runners; they
    are normalised internally so a de-vig rounding error cannot leak into the
    place numbers.
    """
    q = np.asarray(list(win_probs), dtype=float)
    if q.ndim != 1 or q.size == 0:
        raise ValueError("win_probs must be a non-empty 1-D sequence")
    if places not in (2, 3):
        raise ValueError(f"places must be 2 or 3, got {places}")
    if q.size <= places:
        raise ValueError(
            f"a {places}-place market needs more than {places} runners, got {q.size}"
        )
    if np.any(q <= 0):
        raise ValueError("every active runner needs a positive win probability")
    q = q / q.sum()

    qq = q[None, :]
    mask = np.ones_like(qq, dtype=bool)
    pl = np.array([places])

    a = np.power(qq, lam)
    b = np.power(qq, tau)
    A = a.sum(axis=1)[:, None]
    B = b.sum(axis=1)[:, None]
    denom_a = A[:, :, None] - a[:, :, None]
    M = qq[:, :, None] * (a[:, None, :] / denom_a)
    idx = np.arange(q.size)
    M[:, idx, idx] = 0.0
    second = M.sum(axis=1)[0]
    denom_b = B[:, :, None] - b[:, :, None] - b[:, None, :]
    T = np.divide(M, denom_b, out=np.zeros_like(M), where=denom_b > 0)
    T[:, idx, idx] = 0.0
    third_arr = (b * (T.sum(axis=(1, 2))[:, None] - T.sum(axis=2) - T.sum(axis=1)))[0]
    third = third_arr if places == 3 else np.zeros_like(third_arr)

    p_place = place_probabilities_batch(qq, mask, pl, lam, tau)[0]
    return PlaceProbabilities(
        p_place=tuple(float(x) for x in p_place),
        p_first=tuple(float(x) for x in q),
        p_second=tuple(float(x) for x in second),
        p_third=tuple(float(x) for x in third),
        places=places,
        lam=lam,
        tau=tau,
    )


def place_probabilities_loop(
    win_probs, places: int, lam: float, tau: float
) -> tuple[float, ...]:
    """Straightforward loop implementation, kept as a test oracle.

    Same maths as :func:`place_probabilities`, written the obvious way so the
    vectorised version has something independent to be checked against.
    """
    q = np.asarray(list(win_probs), dtype=float)
    q = q / q.sum()
    n = q.size
    a = q**lam
    b = q**tau
    A = a.sum()
    B = b.sum()
    out = [0.0] * n
    for j in range(n):
        out[j] += q[j]
        for i in range(n):
            if i == j:
                continue
            p2 = q[j] * a[i] / (A - a[j])
            out[i] += p2
            if places < 3:
                continue
            for k in range(n):
                if k in (i, j):
                    continue
                out[k] += p2 * b[k] / (B - b[j] - b[i])
    return tuple(out)
