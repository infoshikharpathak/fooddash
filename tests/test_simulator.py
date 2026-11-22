"""The traffic simulator only uses users whose orders are paid at once."""

from services import payments


def test_simulator_never_picks_users_with_real_payments():
    from traffic import simulator

    assert set(payments.USER_MAP) and not set(payments.USER_MAP) & set(simulator._USER_IDS)
    assert len(simulator._USER_IDS) == 20 - len(payments.USER_MAP)
