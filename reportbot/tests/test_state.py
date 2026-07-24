from reportbot.state import StateStore


def test_mark_and_check(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert not store.is_processed("abc")
    store.mark("abc")
    assert store.is_processed("abc")


def test_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).mark("id-1")
    reloaded = StateStore(path)
    assert reloaded.is_processed("id-1")
    assert len(reloaded) == 1


def test_corrupt_file_is_tolerated(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = StateStore(path)  # should not raise
    assert not store.is_processed("anything")
    store.mark("x")
    assert store.is_processed("x")
