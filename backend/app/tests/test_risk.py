"""Fraud-linkage risk scoring tests.

The arithmetic is deliberately closed-form, so most of this is testable without
any datastore - which is the point. A score an investigator cannot recompute by
hand is not explainable.
"""

from __future__ import annotations

import math

import pytest

from app.config import get_settings
from app.services import risk

settings = get_settings()


# ---------------------------------------------------------------------------
# Time decay
# ---------------------------------------------------------------------------
def test_decay_weight_is_one_today():
    assert risk.decay_weight(0, 90) == 1.0


def test_decay_weight_halves_at_one_half_life():
    assert risk.decay_weight(90, 90) == pytest.approx(0.5)


def test_decay_weight_quarters_at_two_half_lives():
    assert risk.decay_weight(180, 90) == pytest.approx(0.25)


def test_decay_weight_is_monotonically_decreasing():
    weights = [risk.decay_weight(d, 90) for d in range(0, 400, 20)]
    assert all(a > b for a, b in zip(weights, weights[1:], strict=False))


def test_decay_weight_never_negative_for_future_dates():
    """Clock skew must not produce a weight above 1 or below 0."""
    assert 0.0 <= risk.decay_weight(-5, 90) <= 1.0


def test_zero_half_life_disables_decay():
    assert risk.decay_weight(1000, 0) == 1.0


def test_recent_case_outweighs_old_case():
    """The core property: recency matters for a repeat-destination signal."""
    recent = risk.decay_weight(7, 90)
    old = risk.decay_weight(365, 90)
    assert recent > old * 4


# ---------------------------------------------------------------------------
# Score shape
# ---------------------------------------------------------------------------
def test_squash_is_bounded_and_monotonic():
    scores = [risk._squash(p) for p in [0, 10, 25, 50, 100, 500, 5000]]
    assert scores[0] == 0.0
    assert all(0.0 <= s <= 100.0 for s in scores)
    assert all(a <= b for a, b in zip(scores, scores[1:], strict=False))


def test_squash_saturates_so_one_reporter_cannot_dominate():
    """The gap between 1 and 5 cases must exceed the gap between 80 and 85."""
    low = risk._squash(5 * risk.BASE_POINTS_PER_CASE) - risk._squash(risk.BASE_POINTS_PER_CASE)
    high = risk._squash(85 * risk.BASE_POINTS_PER_CASE) - risk._squash(
        80 * risk.BASE_POINTS_PER_CASE
    )
    assert low > high


def test_squash_matches_the_documented_formula():
    raw = 37.0
    expected = round(100.0 * (1.0 - math.exp(-raw / risk.SATURATION)), 2)
    assert risk._squash(raw) == expected


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
def test_label_thresholds():
    assert risk.label_for(settings.risk_high_threshold) == "high"
    assert risk.label_for(settings.risk_high_threshold + 5) == "high"
    assert risk.label_for(settings.risk_medium_threshold) == "medium"
    assert risk.label_for(settings.risk_medium_threshold - 0.01) == "low"
    assert risk.label_for(0) == "low"


def test_label_boundaries_are_inclusive_at_the_lower_edge():
    """A score exactly on a threshold takes the higher label - no silent gap."""
    assert risk.label_for(settings.risk_medium_threshold) == "medium"
    assert risk.label_for(settings.risk_high_threshold) == "high"


# ---------------------------------------------------------------------------
# Aggravating factors
# ---------------------------------------------------------------------------
def test_sanctioned_bonus_exceeds_mixer_bonus():
    """An OFAC-listed destination is categorically more severe than mixer contact."""
    assert risk.SANCTIONED_BONUS > risk.MIXER_BONUS


def test_sanctioned_destination_alone_reaches_at_least_medium():
    """Being on the SDN list must not score 'low' just because no case traces there yet."""
    assert risk.label_for(risk._squash(risk.SANCTIONED_BONUS)) in ("medium", "high")


def test_model_version_is_recorded():
    assert risk.MODEL_VERSION
