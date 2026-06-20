from claude_tg.session_store import JsonSessionStore


def test_update_and_load(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    store.update(123, session_id="abc", cwd="/tmp")
    data = store.load()
    assert data["123"]["session_id"] == "abc"
    assert data["123"]["cwd"] == "/tmp"


def test_reset_removes_session_keeps_cwd(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    store.update(1, session_id="x", cwd="/a")
    store.update(1, session_id=None, cwd="/a")
    data = store.load()
    assert "session_id" not in data["1"]
    assert data["1"]["cwd"] == "/a"


def test_corrupt_file_ignored(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("not json{")
    store = JsonSessionStore(p)
    assert store.load() == {}
