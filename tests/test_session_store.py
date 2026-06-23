import json

import pytest

from claude_tg.session_store import (
    DuplicateProject,
    InvalidProjectName,
    JsonSessionStore,
    UnknownProject,
    validate_project_name,
)

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


# ---- T3: SB4 project-name validation ---------------------------------------


@pytest.mark.parametrize("name", ["a", "Work_1", "my-proj", "x" * 32])
def test_validate_project_name_accepts_valid(name):
    """SB4 accepts non-empty, ≤32-char ASCII letter/digit/underscore/hyphen names."""
    validate_project_name(name)  # must not raise


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "x" * 33,  # too long
        "has space",  # space
        "dot.name",  # dot
        "../x",  # path traversal
        "a/b",  # slash
        "café",  # unicode
        "name!",  # punctuation
        "a\nb",  # embedded newline (must not slip past a $-anchored regex)
        "abc\n",  # trailing newline — the classic re `$`-before-`\n` hole; fullmatch closes it
        "\x00",  # null byte
        "a\x00b",  # embedded null byte
        " x",  # leading whitespace
        "x ",  # trailing whitespace
        "a\tb",  # tab
        "a\r\nb",  # CRLF
    ],
)
def test_validate_project_name_rejects_invalid(name):
    """SB4 rejects empty / >32 / spaces / dots / slashes / unicode / punctuation /
    and (anti-regression) embedded or trailing newline, null byte, CRLF, tab, and
    leading/trailing whitespace — i.e. `fullmatch`, not `search`, anchors the rule."""
    with pytest.raises(InvalidProjectName):
        validate_project_name(name)


# ---- T3: create / list / get -----------------------------------------------


def test_create_list_get_happy_path(tmp_path):
    """create() adds a project, makes it active by default, and is visible via
    list_projects()/get_project()/get_active()."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "Work", "/work")

    assert store.get_active(1) == "Work"
    projects = store.list_projects(1)
    assert set(projects) == {"Work"}
    assert projects["Work"]["cwd"] == "/work"
    assert projects["Work"]["session_id"] is None
    assert projects["Work"]["created_at"] and projects["Work"]["last_active"]

    rec = store.get_project(1, "Work")
    assert rec is not None and rec["cwd"] == "/work"


def test_create_make_active_false_leaves_active_unchanged(tmp_path):
    """create(make_active=False) adds the project but does not change active."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "Alpha", "/a")  # active -> Alpha
    store.create(1, "Beta", "/b", make_active=False)

    assert store.get_active(1) == "Alpha"
    assert set(store.list_projects(1)) == {"Alpha", "Beta"}


def test_create_first_project_make_active_false_active_stays_none(tmp_path):
    """create(make_active=False) on a fresh chat leaves active None (no auto-pick)."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "Solo", "/s", make_active=False)
    assert store.get_active(1) is None
    assert set(store.list_projects(1)) == {"Solo"}


def test_create_duplicate_is_case_insensitive(tmp_path):
    """A second create() with a case-variant name raises DuplicateProject."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "Work", "/work")
    with pytest.raises(DuplicateProject):
        store.create(1, "work", "/other")
    # The original survives untouched; no second project was written.
    projects = store.list_projects(1)
    assert set(projects) == {"Work"}
    assert projects["Work"]["cwd"] == "/work"


def test_create_invalid_name_raises_without_writing(tmp_path):
    """An SB4-invalid name raises InvalidProjectName and writes nothing."""
    p = tmp_path / "s.json"
    store = JsonSessionStore(p)
    with pytest.raises(InvalidProjectName):
        store.create(1, "bad name", "/x")
    assert not p.exists()  # rejected before any write
    assert store.list_projects(1) == {}


def test_list_projects_empty_for_unknown_chat(tmp_path):
    """list_projects() on a chat with no registry entry is an empty dict."""
    store = JsonSessionStore(tmp_path / "s.json")
    assert store.list_projects(999) == {}


def test_get_project_case_insensitive_and_missing(tmp_path):
    """get_project() matches case-insensitively; returns None when absent."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "MyProj", "/m")
    assert store.get_project(1, "myproj")["cwd"] == "/m"
    assert store.get_project(1, "nope") is None


def test_list_projects_copy_does_not_mutate_store(tmp_path):
    """Mutating the dict returned by list_projects() does not persist."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    snapshot = store.list_projects(1)
    del snapshot["A"]  # mutate the caller's copy
    assert set(store.list_projects(1)) == {"A"}  # store unaffected


# ---- T3: switch ------------------------------------------------------------


def test_switch_sets_active(tmp_path):
    """switch() changes the chat's active project."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    store.create(1, "B", "/b", make_active=False)
    store.switch(1, "B")
    assert store.get_active(1) == "B"


def test_switch_is_case_insensitive_preserves_stored_case(tmp_path):
    """switch('work') resolves to the stored 'Work' and sets active to that case."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "Work", "/work", make_active=False)
    store.switch(1, "work")
    assert store.get_active(1) == "Work"  # stored case, not the requested case


def test_switch_unknown_raises(tmp_path):
    """switch() to a non-existent project raises UnknownProject."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    with pytest.raises(UnknownProject):
        store.switch(1, "nope")


def test_switch_unknown_chat_raises(tmp_path):
    """switch() on a chat with no registry entry raises UnknownProject."""
    store = JsonSessionStore(tmp_path / "s.json")
    with pytest.raises(UnknownProject):
        store.switch(404, "anything")


# ---- T3: remove ------------------------------------------------------------


def test_remove_deletes_project(tmp_path):
    """remove() drops the project from the registry."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    store.create(1, "B", "/b", make_active=False)
    store.remove(1, "B")
    assert set(store.list_projects(1)) == {"A"}
    assert store.get_active(1) == "A"  # untouched


def test_remove_active_clears_active(tmp_path):
    """Removing the active project sets active -> None (store stays consistent)."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")  # active
    store.remove(1, "A")
    assert store.get_active(1) is None
    assert store.list_projects(1) == {}


def test_remove_active_case_insensitive_clears_active(tmp_path):
    """Removing the active project by a case-variant name still clears active."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "Work", "/work")  # active == "Work"
    store.remove(1, "work")
    assert store.get_active(1) is None


def test_remove_unknown_raises(tmp_path):
    """remove() of a non-existent project raises UnknownProject."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    with pytest.raises(UnknownProject):
        store.remove(1, "nope")


# ---- T3: touch -------------------------------------------------------------


def test_touch_updates_last_active(tmp_path):
    """touch() advances last_active (and leaves created_at alone)."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    before = store.get_project(1, "A")
    created_at = before["created_at"]

    # Force a distinct timestamp without sleeping: rewrite last_active to the past.
    raw = store._load_raw()
    raw["chats"]["1"]["projects"]["A"]["last_active"] = "2000-01-01T00:00:00+00:00"
    store._save_raw(raw)

    store.touch(1, "a")  # case-insensitive
    after = store.get_project(1, "A")
    assert after["last_active"] != "2000-01-01T00:00:00+00:00"
    assert after["created_at"] == created_at  # unchanged


def test_touch_unknown_raises(tmp_path):
    """touch() of a non-existent project raises UnknownProject."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    with pytest.raises(UnknownProject):
        store.touch(1, "nope")


# ---- T3: additivity guard (the flat one-shot view is undisturbed) ----------


def test_registry_create_is_invisible_to_flat_view_until_it_has_content(tmp_path):
    """A project created with cwd is visible via the flat load() (cwd is truthy);
    a project with neither session_id nor cwd is omitted (pre-P4 'pop empty')."""
    store = JsonSessionStore(tmp_path / "s.json")
    # cwd-only project -> flat view exposes the active project's cwd.
    store.create(1, "A", "/a")
    assert store.load()["1"] == {"cwd": "/a"}

    # A make_active=False project on a fresh chat leaves active None -> absent.
    store2 = JsonSessionStore(tmp_path / "s2.json")
    store2.create(2, "B", "/b", make_active=False)
    assert "2" not in store2.load()


def test_registry_and_flat_view_interoperate(tmp_path):
    """The flat update() and the registry switch() agree on the active project:
    update() writes the active project's session_id; switching changes which
    project the flat view reflects."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")  # active == A
    store.create(1, "B", "/b", make_active=False)

    store.update(1, session_id="sid-a", cwd=None)  # writes A (the active one)
    assert store.get_project(1, "A")["session_id"] == "sid-a"
    assert store.get_project(1, "B")["session_id"] is None
    assert store.load()["1"] == {"session_id": "sid-a", "cwd": "/a"}

    store.switch(1, "B")  # flat view now follows B
    assert store.load()["1"] == {"cwd": "/b"}  # B has no session yet
