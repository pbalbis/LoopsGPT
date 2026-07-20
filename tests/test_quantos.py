import quantos


def test_hash_is_order_independent():
    assert quantos.stable_hash({"a":1,"b":2}) == quantos.stable_hash({"b":2,"a":1})


def test_profit_factor():
    assert quantos.profit_factor([2,-1,3,-1]) == 2.5
    assert quantos.profit_factor([]) is None


def test_drawdown():
    assert round(quantos.max_drawdown([100,110,99,120]), 4) == 0.1


def test_mode_schedule():
    assert quantos.mode_for({"run_id":0,"stall_count":0,"deep_triggers":[]}) == "LIGHT"
    assert quantos.mode_for({"run_id":23,"stall_count":0,"deep_triggers":[]}) == "MID"
    assert quantos.mode_for({"run_id":1,"stall_count":3,"deep_triggers":[]}) == "DEEP"
