from fractions import Fraction

from finround.flow import Circulation


def test_lower_bounds_force_flow():
    net = Circulation(2)
    a = net.add_edge(0, 1, 3, 5, 1)
    b = net.add_edge(1, 0, 0, 10, 0)
    assert net.solve()
    assert net.flow(a) == 3  # cost 1 per unit, so no more than forced
    assert net.flow(b) == 3
    assert net.cost == 3


def test_negative_cost_arcs_are_used_to_the_limit():
    net = Circulation(2)
    a = net.add_edge(0, 1, 0, 4, -2)
    net.add_edge(1, 0, 0, 10, 0)
    assert net.solve()
    assert net.flow(a) == 4
    assert net.cost == -8


def test_infeasible_lower_bound_is_reported():
    net = Circulation(2)
    net.add_edge(0, 1, 3, 5, 0)
    net.add_edge(1, 0, 0, 2, 0)  # cannot return the 3 units
    assert not net.solve()


def test_negative_flows_are_allowed():
    """Cells of a table can be negative, so arcs must carry negative flow."""
    net = Circulation(2)
    a = net.add_edge(0, 1, -5, -2, 1)
    b = net.add_edge(1, 0, -10, 10, 0)
    assert net.solve()
    assert net.flow(a) == -5  # cost 1 per unit, so as low as the bound allows
    assert net.flow(b) == -5


def test_costs_may_be_fractions():
    net = Circulation(3)
    cheap = net.add_edge(0, 1, 0, 1, Fraction(-1, 3))
    dear = net.add_edge(0, 2, 0, 1, Fraction(1, 5))
    net.add_edge(1, 0, 0, 5, 0)
    net.add_edge(2, 0, 0, 5, 0)
    assert net.solve()
    assert net.flow(cheap) == 1
    assert net.flow(dear) == 0
    assert net.cost == Fraction(-1, 3)


def test_rejects_upper_below_lower():
    net = Circulation(2)
    try:
        net.add_edge(0, 1, 5, 1)
    except ValueError:
        return
    raise AssertionError("expected a ValueError")
