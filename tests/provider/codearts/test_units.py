"""CodeArts token→积分折算。"""

from __future__ import annotations

import pytest


def test_units_tokens_to_credits():
    from src.provider.codearts.units import (
        DAILY_POOL_CREDITS,
        DAILY_POOL_TOKENS,
        TOKENS_PER_CREDIT,
        tokens_to_credits,
    )

    assert TOKENS_PER_CREDIT == 10_000
    assert tokens_to_credits(DAILY_POOL_TOKENS) == DAILY_POOL_CREDITS == 1000
    assert tokens_to_credits(0) == 0
    assert tokens_to_credits(726) == pytest.approx(0.0726)
