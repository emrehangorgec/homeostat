from homeostat import m0


def test_broadcast_result_data_is_extracted():
    out = 'Broadcasting: Intent { act=com.homeostat.agent.PROBE }\nBroadcast completed: result=0, data="{"a":1}"\n'
    assert m0._broadcast_data(out) == '{"a":1}'
    assert m0._broadcast_data("Broadcast completed: result=0\n") is None


def test_table_lists_every_capability():
    text = m0.table({"model": "OnePlus A5010", "sdk": 29, "device_owner": {"ok": False, "detail": "not device owner"}})
    assert "OnePlus A5010 (API 29)" in text
    assert "| device_owner | NO | not device owner |" in text
