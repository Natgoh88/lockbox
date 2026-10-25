"""The points shown for each part of the score add up to the score. Display only: the score is unchanged."""
import re

from lockbox.inflows import Inflow
from lockbox.underwrite import Policy, shown_points, underwrite

NOW, IV = 1_800_000_000, 120


def _points(o):
    return [int(m.group(1)) for r in o.reasons for m in [re.search(r": (\d+)/\d+$", r)] if m]


def test_largest_remainder_keeps_the_total():
    # each part rounded on its own would show 15+23+15+20 = 73 next to a score of 72
    parts = [14.58, 22.9, 15.1, 19.6]
    assert sum(round(x) for x in parts) == 73 and round(sum(parts)) == 72
    assert shown_points(parts, 72) == [14, 23, 15, 20]


def test_demo_quote_points_add_up(sc):
    o = sc.quote()
    assert len(_points(o)) == 4 and sum(_points(o)) == o.score


def test_uneven_histories_add_up():
    for amounts in ([3, 9, 4, 12, 5, 8], [10, 10, 10, 10, 10, 10], [1, 30, 2, 25, 3, 40], [7, 7, 20, 2, 9, 13]):
        flows = [Inflow(NOW - (6 - k) * IV + 5 + i, a * 1_000_000 + i * 333_333, s)
                 for k, a in enumerate(amounts) for i, s in enumerate("abc")]
        o = underwrite(flows, NOW, Policy(payment_interval=IV, lookback_buckets=12))
        assert sum(_points(o)) == o.score, (amounts, o.reasons, o.score)
