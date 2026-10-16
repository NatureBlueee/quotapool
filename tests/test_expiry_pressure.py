from dataclasses import replace

from claude_fleet.optimizer import OptimizationAccount, expiry_pressure


NOW = 1_000_000.0
HOUR = 3600


def account(**changes):
    return replace(OptimizationAccount(
        account_id="soon", five_remaining=0.25,
        five_reset_at=NOW + 2 * HOUR, weekly_remaining=0.42,
        weekly_reset_at=NOW + HOUR / 2,
    ), **changes)


def pressure(value, burn=0.04):
    return expiry_pressure(value, now=NOW,
                           five_to_weekly_capacity=0.2,
                           weekly_burn_per_hour=burn)


def test_weekly_risk_is_capped_by_reachable_five_hour_capacity():
    risk = pressure(account())
    assert risk["bucket"] == "weekly"
    assert risk["spendable_weekly_fraction"] == 0.05
    assert risk["forecast_demand_fraction"] == 0.02
    assert risk["at_risk_weekly_fraction"] == 0.03


def test_coverable_demand_needs_no_expiry_override():
    assert pressure(account(), burn=0.2) == {}


def test_unknown_or_past_deadlines_do_not_invent_urgency():
    assert pressure(account(five_reset_at=None, weekly_reset_at=None)) == {}
    assert pressure(account(five_reset_at=NOW, weekly_reset_at=NOW - 1)) == {}


def test_nearly_spent_account_gets_no_boost_below_reporting_resolution():
    assert pressure(account(five_remaining=0.01), burn=0) == {}


def test_weekly_remaining_caps_five_hour_risk():
    risk = pressure(account(five_remaining=1, weekly_remaining=0.04,
                            weekly_reset_at=NOW + 7 * 24 * HOUR,
                            five_reset_at=NOW + HOUR / 2))
    assert risk["bucket"] == "five_hour"
    assert risk["spendable_weekly_fraction"] == 0.04


def test_imminent_observed_renewal_preserves_floor_and_weekly_cap():
    risk = pressure(account(five_remaining=0.1, five_floor=0.4,
                            five_reset_at=NOW + HOUR / 2,
                            weekly_reset_at=NOW + 2 * HOUR), burn=0.05)
    assert risk["bucket"] == "weekly"
    assert risk["spendable_weekly_fraction"] == 0.14
    assert risk["at_risk_weekly_fraction"] == 0.04


def test_distant_weekly_reset_is_not_an_imminent_expiry():
    assert pressure(account(five_reset_at=None,
                            weekly_reset_at=NOW + 16 * HOUR)) == {}
