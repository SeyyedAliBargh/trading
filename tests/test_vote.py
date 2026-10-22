"""Tests for ai/vote.py (STEP 4d PART 6)."""
import pytest

from ai.models import ModelVote
from ai.vote import VoteResult, tally


def vote(model, action, conf=0.7, stop=0.015, target=0.045, valid=True, error=None):
    return ModelVote(model=model, action=action, confidence=conf, stop_pct=stop,
                     target_pct=target, reason="test", valid=valid, error=error)


class TestTally:
    def test_three_zero_buy(self):
        votes = [vote("m1", "BUY", 0.6, 0.010, 0.030),
                 vote("m2", "BUY", 0.7, 0.020, 0.050),
                 vote("m3", "BUY", 0.8, 0.030, 0.070)]
        res = tally(votes, 2, True)
        assert res.action == "BUY"
        assert res.confidence_mean == pytest.approx(0.7)
        assert res.stop_pct_median == pytest.approx(0.020)
        assert res.target_pct_median == pytest.approx(0.050)
        assert res.agreeing_models == ["m1", "m2", "m3"]
        assert res.reasons

    def test_two_one_buy(self):
        votes = [vote("m1", "BUY"), vote("m2", "BUY"), vote("m3", "HOLD")]
        res = tally(votes, 2, True)
        assert res.action == "BUY"
        assert res.agreeing_models == ["m1", "m2"]
        # medians/means only over the agreeing models
        assert res.confidence_mean == pytest.approx(0.7)

    def test_one_one_one_hold(self):
        votes = [vote("m1", "BUY"), vote("m2", "SELL"), vote("m3", "HOLD")]
        res = tally(votes, 2, True)
        assert res.action == "HOLD"
        assert res.agreeing_models == []
        assert any("no action reached" in r for r in res.reasons)

    def test_invalid_votes_all_hold(self):
        votes = [vote("m1", "HOLD", valid=False, error="timeout"),
                 vote("m2", "HOLD", valid=False, error="bad json"),
                 vote("m3", "HOLD", valid=False, error="http_500")]
        res = tally(votes, 2, True)
        assert res.action == "HOLD"
        assert any("invalid vote" in r for r in res.reasons)

    def test_invalid_votes_count_as_hold_against_buy(self):
        votes = [vote("m1", "BUY"), vote("m2", "HOLD", valid=False)]
        res = tally(votes, 2, True)
        assert res.action == "HOLD"  # 1 BUY < min 2

    def test_two_valid_buy_beats_one_invalid(self):
        votes = [vote("m1", "BUY"), vote("m2", "BUY"),
                 vote("m3", "HOLD", valid=False)]
        res = tally(votes, 2, True)
        assert res.action == "BUY"
        assert res.agreeing_models == ["m1", "m2"]

    def test_medians_of_agreeing_only(self):
        votes = [vote("m1", "BUY", 0.5, 0.01, 0.02),
                 vote("m2", "BUY", 0.9, 0.02, 0.04),
                 vote("m3", "BUY", 0.7, 0.03, 0.06),
                 vote("m4", "HOLD", 0.1, 9.99, 9.99)]  # must not affect medians
        res = tally(votes, 3, True)
        assert res.action == "BUY"
        assert res.stop_pct_median == pytest.approx(0.02)
        assert res.target_pct_median == pytest.approx(0.04)
        assert res.confidence_mean == pytest.approx((0.5 + 0.9 + 0.7) / 3)

    def test_sell_majority_allowed_when_models_vote_on_exit(self):
        votes = [vote("m1", "SELL"), vote("m2", "SELL"), vote("m3", "HOLD")]
        res = tally(votes, 2, True)
        assert res.action == "SELL"
        assert res.agreeing_models == ["m1", "m2"]

    def test_sell_majority_blocked_when_models_do_not_vote_on_exit(self):
        votes = [vote("m1", "SELL"), vote("m2", "SELL"), vote("m3", "SELL")]
        res = tally(votes, 2, False)
        assert res.action == "HOLD"
        assert any("MODELS_VOTE_ON_EXIT" in r for r in res.reasons)

    def test_buy_unaffected_by_exit_flag(self):
        votes = [vote("m1", "BUY"), vote("m2", "BUY")]
        res = tally(votes, 2, False)
        assert res.action == "BUY"

    def test_min_agreeing_not_reached(self):
        votes = [vote("m1", "BUY"), vote("m2", "HOLD"), vote("m3", "HOLD")]
        res = tally(votes, 2, True)
        assert res.action == "HOLD"

    def test_tie_buy_sell_is_hold(self):
        votes = [vote("m1", "BUY"), vote("m2", "BUY"),
                 vote("m3", "SELL"), vote("m4", "SELL")]
        res = tally(votes, 2, True)
        assert res.action == "HOLD"
        assert any("tie" in r for r in res.reasons)

    def test_larger_side_wins(self):
        votes = [vote("m1", "BUY"), vote("m2", "BUY"), vote("m3", "BUY"),
                 vote("m4", "SELL"), vote("m5", "SELL")]
        res = tally(votes, 2, True)
        assert res.action == "BUY"

    def test_empty_votes(self):
        res = tally([], 2, True)
        assert res.action == "HOLD"
        assert res.agreeing_models == []
        assert any("no votes" in r for r in res.reasons)

    def test_result_is_frozen(self):
        res = tally([vote("m1", "BUY")], 1, True)
        assert isinstance(res, VoteResult)
        with pytest.raises(Exception):
            res.action = "SELL"
