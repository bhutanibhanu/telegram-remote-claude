import json

from claude_tg.session_store import JsonSessionStore

# ---- pre-P4 flat-view contract (the live one-shot bot depends on this) -----


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


def test_state_file_permissions(tmp_path):
    p = tmp_path / "s.json"
    JsonSessionStore(p).update(1, session_id="x", cwd="/a")
    assert (p.stat().st_mode & 0o777) == 0o600


# ---- flat-view regression (THE critical guard) -----------------------------


def test_flat_view_roundtrip_matches_pre_p4_contract(tmp_path):
    """A round-trip through update()+load() reproduces the pre-P4 flat shape:
    exactly ``{"session_id": ..., "cwd": ...}`` per chat — no v2 fields leak."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.update(42, session_id="sid", cwd="/cwd")
    assert store.load()["42"] == {"session_id": "sid", "cwd": "/cwd"}


def test_cwd_none_leaves_cwd_unchanged(tmp_path):
    """``cwd=None`` must not clobber a stored cwd (pre-P4 semantics)."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.update(7, session_id="s1", cwd="/work")
    store.update(7, session_id="s2", cwd=None)
    data = store.load()
    assert data["7"]["session_id"] == "s2"
    assert data["7"]["cwd"] == "/work"


def test_multiple_chats_stay_independent(tmp_path):
    """Each chat's active project is isolated — writing one never disturbs another."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.update(1, session_id="s1", cwd="/one")
    store.update(2, session_id="s2", cwd="/two")
    store.update(1, session_id=None, cwd="/one")  # clear chat 1's session
    data = store.load()
    assert "session_id" not in data["1"]
    assert data["1"]["cwd"] == "/one"
    assert data["2"] == {"session_id": "s2", "cwd": "/two"}


def test_empty_active_project_drops_out_of_flat_view(tmp_path):
    """A chat whose active project has neither session_id nor cwd is absent from
    the flat view (mirrors pre-P4 "pop empty" -> no entry)."""
    store = JsonSessionStore(tmp_path / "s.json")
    # No cwd, then clear the session -> active project carries nothing observable.
    store.update(9, session_id="s", cwd=None)
    store.update(9, session_id=None, cwd=None)
    assert "9" not in store.load()


def test_no_active_project_absent_from_flat_view(tmp_path):
    """A chat present in the registry but with no active project is omitted."""
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "chats": {
                    "5": {
                        "active": None,
                        "projects": {
                            "alpha": {
                                "cwd": "/a",
                                "session_id": "sid",
                                "created_at": "2026-01-01T00:00:00+00:00",
                                "last_active": "2026-01-01T00:00:00+00:00",
                            }
                        },
                    }
                },
            }
        )
    )
    assert JsonSessionStore(p).load() == {}


# ---- v1 -> v2 migration -----------------------------------------------------


def test_v1_to_v2_migration_wraps_default_project(tmp_path):
    """A legacy flat file migrates on load to v2 with a `default` active project
    that preserves session_id + cwd and gains timestamps."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"123": {"session_id": "abc", "cwd": "/tmp"}}))
    store = JsonSessionStore(p)
    raw = store._load_raw()

    assert raw["version"] == 2
    chat = raw["chats"]["123"]
    assert chat["active"] == "default"
    proj = chat["projects"]["default"]
    assert proj["session_id"] == "abc"
    assert proj["cwd"] == "/tmp"
    assert proj["created_at"] and proj["last_active"]


def test_v1_migration_preserves_flat_view(tmp_path):
    """The migrated doc still answers the flat view with the original entry."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"77": {"session_id": "sid", "cwd": "/proj"}}))
    assert JsonSessionStore(p).load()["77"] == {"session_id": "sid", "cwd": "/proj"}


def test_migration_idempotent(tmp_path):
    """Migrate v1 -> v2, persist, reload: the second load is a no-op (equal doc)."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"1": {"session_id": "x", "cwd": "/a"}}))
    store = JsonSessionStore(p)

    migrated = store._load_raw()
    store._save_raw(migrated)  # now v2 on disk
    reloaded = store._load_raw()  # pure v2 read -> must not re-migrate / mutate
    assert reloaded == migrated
    assert reloaded["version"] == 2


def test_v1_migration_skips_non_dict_entries(tmp_path):
    """A v1 doc with a junk (non-dict) entry migrates the good ones, skips junk."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"1": {"session_id": "x", "cwd": "/a"}, "2": "junk"}))
    raw = JsonSessionStore(p)._load_raw()
    assert "1" in raw["chats"]
    assert "2" not in raw["chats"]


# ---- corrupt / unknown-version fail-safe ------------------------------------


def test_unknown_version_loads_empty(tmp_path):
    """A future/unknown schema version fails safe to empty (flat) without raising."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"version": 99, "chats": {"1": {"active": "x"}}}))
    store = JsonSessionStore(p)
    assert store.load() == {}
    assert store._load_raw() == {"version": 2, "chats": {}}


def test_corrupt_load_raw_returns_empty_v2(tmp_path):
    """Corrupt JSON yields a well-formed empty v2 doc from _load_raw (no raise)."""
    p = tmp_path / "s.json"
    p.write_text("}{ not json")
    assert JsonSessionStore(p)._load_raw() == {"version": 2, "chats": {}}


def test_non_dict_json_loads_empty(tmp_path):
    """A JSON array (valid JSON, wrong type) fails safe to empty."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps([1, 2, 3]))
    store = JsonSessionStore(p)
    assert store.load() == {}
    assert store._load_raw() == {"version": 2, "chats": {}}


def test_missing_file_loads_empty(tmp_path):
    """No file on disk -> empty flat view and empty v2 raw (no raise)."""
    store = JsonSessionStore(tmp_path / "does-not-exist.json")
    assert store.load() == {}
    assert store._load_raw() == {"version": 2, "chats": {}}


# ---- atomicity + perms + schema on write ------------------------------------


def test_update_writes_versioned_v2_atomic_and_0600(tmp_path):
    """After update(): the file exists, is valid JSON with version==2, holds the
    data under a default active project, leaves no .tmp, and is mode 0600."""
    p = tmp_path / "s.json"
    store = JsonSessionStore(p)
    store.update(1, session_id="sid", cwd="/a")

    assert p.is_file()
    assert not p.with_name(p.name + ".tmp").exists()  # temp swapped in, not left
    assert (p.stat().st_mode & 0o777) == 0o600

    on_disk = json.loads(p.read_text())
    assert on_disk["version"] == 2
    proj = on_disk["chats"]["1"]["projects"]["default"]
    assert on_disk["chats"]["1"]["active"] == "default"
    assert proj["session_id"] == "sid"
    assert proj["cwd"] == "/a"


def test_save_raw_roundtrips(tmp_path):
    """_save_raw then _load_raw returns the same v2 document."""
    p = tmp_path / "s.json"
    store = JsonSessionStore(p)
    doc = {
        "version": 2,
        "chats": {
            "1": {
                "active": "alpha",
                "projects": {
                    "alpha": {
                        "cwd": "/a",
                        "session_id": "sid",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "last_active": "2026-01-02T00:00:00+00:00",
                    }
                },
            }
        },
    }
    store._save_raw(doc)
    assert store._load_raw() == doc
