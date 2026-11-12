from src.labeller import MintEvent, collapse_pct, label_outcome


def test_collapse_pct():
    assert collapse_pct(MintEvent(100000, 2000)) == 98.0
    assert collapse_pct(MintEvent(None, 2000)) is None
    assert collapse_pct(MintEvent(0, 0)) is None


def test_rugged_fast_collapse_with_lp_removal():
    assert label_outcome(MintEvent(420000, 8000, lp_removed=True,
                                   dev_sold_pct=10, time_to_collapse_minutes=35)) == "rugged"


def test_rugged_fast_collapse_with_big_dev_sell():
    assert label_outcome(MintEvent(420000, 8000, lp_removed=False,
                                   dev_sold_pct=82, time_to_collapse_minutes=35)) == "rugged"


def test_rugged_huge_collapse_with_modest_dev_sell():
    assert label_outcome(MintEvent(100000, 3000, lp_removed=False,
                                   dev_sold_pct=25, time_to_collapse_minutes=5000)) == "rugged"


def test_failed_but_honest_no_lp_no_dev_sell():
    assert label_outcome(MintEvent(100000, 20000, lp_removed=False,
                                   dev_sold_pct=5, time_to_collapse_minutes=60)) == "failed_but_honest"


def test_failed_but_honest_slow_bleed():
    assert label_outcome(MintEvent(100000, 20000, lp_removed=False,
                                   dev_sold_pct=30, time_to_collapse_minutes=5000)) == "failed_but_honest"


def test_inconclusive_missing_data():
    assert label_outcome(MintEvent(None, None)) == "inconclusive"
    assert label_outcome(MintEvent(100000, 95000)) == "inconclusive"


def test_inconclusive_small_collapse_even_with_dev_sell():
    # 20% fall is not a rug by any rule, even with a dev sell.
    assert label_outcome(MintEvent(100000, 80000, dev_sold_pct=60,
                                   time_to_collapse_minutes=30)) == "inconclusive"
