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


# ---- P5/T7: set_session_id — targeted per-project session write -------------


def test_set_session_id_writes_named_project_not_active(tmp_path):
    """set_session_id targets the NAMED project — even when a DIFFERENT one is active.

    This is the store half of the P5 per-project persist: with /switch free, a turn's
    result must land on the project it ran on, not on whatever is active when it lands."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a", make_active=True)
    store.create(1, "B", "/b", make_active=False)
    store.switch(1, "B")  # B is active; we still write A explicitly

    store.set_session_id(1, "A", "sid-a")
    assert store.get_project(1, "A")["session_id"] == "sid-a"  # the NAMED project
    assert store.get_project(1, "B")["session_id"] is None  # active B untouched
    assert store.get_project(1, "A")["cwd"] == "/a"  # cwd left untouched (D4)
    assert store.get_active(1) == "B"  # active unchanged


def test_set_session_id_none_clears(tmp_path):
    """set_session_id(None) clears the named project's session_id (reset / dead resume)."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    store.set_session_id(1, "A", "sid-a")
    store.set_session_id(1, "A", None)
    assert store.get_project(1, "A")["session_id"] is None


def test_set_session_id_case_insensitive(tmp_path):
    """set_session_id matches the name case-insensitively (like the rest of the CRUD)."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "Work", "/w")
    store.set_session_id(1, "WORK", "sid")  # casing variant
    assert store.get_project(1, "Work")["session_id"] == "sid"


def test_set_session_id_unknown_raises(tmp_path):
    """set_session_id of a non-existent project raises UnknownProject (real error)."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    with pytest.raises(UnknownProject):
        store.set_session_id(1, "nope", "sid")


def test_set_session_id_bumps_last_active(tmp_path):
    """set_session_id advances last_active (a turn result is recent activity)."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "A", "/a")
    raw = store._load_raw()
    raw["chats"]["1"]["projects"]["A"]["last_active"] = "2000-01-01T00:00:00+00:00"
    store._save_raw(raw)
    store.set_session_id(1, "A", "sid")
    assert store.get_project(1, "A")["last_active"] != "2000-01-01T00:00:00+00:00"


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


# ---- P9 / T3 — per-project cumulative cost (add_cost / get_cost) ------------


def test_add_cost_accumulates_and_persists_across_reload(tmp_path):
    """add_cost accumulates per project and survives a store reload (RB6)."""
    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.create(1, "alpha", "/work/alpha", make_active=True)
    assert store.add_cost(1, "alpha", 0.012) == pytest.approx(0.012)
    assert store.add_cost(1, "alpha", 0.008) == pytest.approx(0.02)
    # A fresh store over the same file reads the accumulated total back.
    reloaded = JsonSessionStore(path)
    assert reloaded.get_cost(1, "alpha") == pytest.approx(0.02)


def test_add_cost_is_case_insensitive(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "Alpha", "/work/alpha", make_active=True)
    store.add_cost(1, "alpha", 0.5)
    store.add_cost(1, "ALPHA", 0.25)
    assert store.get_cost(1, "Alpha") == pytest.approx(0.75)


def test_add_cost_ignores_bad_values(tmp_path):
    """NaN / inf / negative deltas are ignored (RB1) — the running total never corrupts."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.add_cost(1, "alpha", 1.0)
    assert store.add_cost(1, "alpha", float("nan")) == pytest.approx(1.0)
    assert store.add_cost(1, "alpha", float("inf")) == pytest.approx(1.0)
    assert store.add_cost(1, "alpha", -5.0) == pytest.approx(1.0)
    assert store.get_cost(1, "alpha") == pytest.approx(1.0)


def test_add_cost_unknown_project_raises(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    with pytest.raises(UnknownProject):
        store.add_cost(1, "nope", 1.0)


def test_get_cost_unknown_or_unset_is_zero(tmp_path):
    """get_cost is read-only and never raises — unknown project / unset field → 0.0."""
    store = JsonSessionStore(tmp_path / "state.json")
    assert store.get_cost(1, "nope") == 0.0
    store.create(1, "alpha", "/work/alpha", make_active=True)
    assert store.get_cost(1, "alpha") == 0.0  # never charged yet


def test_add_cost_writes_0600(tmp_path):
    import os
    import stat

    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.add_cost(1, "alpha", 0.1)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ---- T4 (P9): per-project model override --------------------------------------


def test_set_get_model_roundtrip_and_persist(tmp_path):
    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.create(1, "alpha", "/work/alpha", make_active=True)
    assert store.get_model(1, "alpha") is None  # no override by default
    store.set_model(1, "alpha", "claude-haiku-4-5")
    assert store.get_model(1, "alpha") == "claude-haiku-4-5"
    # Persists across a fresh store over the same file (RB6).
    store2 = JsonSessionStore(path)
    assert store2.get_model(1, "alpha") == "claude-haiku-4-5"


def test_set_model_clear_removes_override(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.set_model(1, "alpha", "claude-opus-4-8")
    assert store.get_model(1, "alpha") == "claude-opus-4-8"
    store.set_model(1, "alpha", None)  # /auto clears
    assert store.get_model(1, "alpha") is None


def test_set_model_case_insensitive_and_empty_clears(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "Alpha", "/work/alpha", make_active=True)
    store.set_model(1, "alpha", "claude-opus-4-8")  # case-insensitive match
    assert store.get_model(1, "ALPHA") == "claude-opus-4-8"
    store.set_model(1, "alpha", "   ")  # whitespace/empty normalizes to a clear
    assert store.get_model(1, "alpha") is None


def test_set_model_unknown_project_raises(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    with pytest.raises(UnknownProject):
        store.set_model(1, "nope", "claude-haiku-4-5")


def test_get_model_unknown_or_unset_is_none(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    assert store.get_model(1, "nope") is None
    store.create(1, "alpha", "/work/alpha", make_active=True)
    assert store.get_model(1, "alpha") is None


def test_set_model_writes_0600(tmp_path):
    import os
    import stat

    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.set_model(1, "alpha", "claude-opus-4-8")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ---- T5 (P9): per-chat macros -------------------------------------------------


def test_save_run_macro_roundtrip_and_persist(tmp_path):
    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.save_macro(1, "deploy", "run the deploy for $1")
    assert store.get_macro(1, "deploy") == "run the deploy for $1"
    # Persists across reload (RB6).
    store2 = JsonSessionStore(path)
    assert store2.get_macro(1, "deploy") == "run the deploy for $1"


def test_get_macro_case_insensitive(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.save_macro(1, "Deploy", "go")
    assert store.get_macro(1, "deploy") == "go"
    assert store.get_macro(1, "DEPLOY") == "go"


@pytest.mark.parametrize("bad", ["../etc", "has space", "x" * 33, "", "a/b", "..", "naïve"])
def test_save_macro_rejects_bad_name_sb4(tmp_path, bad):
    store = JsonSessionStore(tmp_path / "state.json")
    with pytest.raises(InvalidProjectName):
        store.save_macro(1, bad, "body")
    # Nothing was persisted (the validate happens before any write).
    assert store.list_macros(1) == {}


def test_list_macros_and_overwrite(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.save_macro(1, "a", "first")
    store.save_macro(1, "b", "second")
    assert store.list_macros(1) == {"a": "first", "b": "second"}
    store.save_macro(1, "a", "updated")  # overwrite same name
    assert store.list_macros(1)["a"] == "updated"


def test_save_macro_case_insensitive_overwrite_no_duplicate(tmp_path):
    # RED-GREEN (P0 macro case-collision): get_macro/remove_macro resolve case-insensitively,
    # but save_macro wrote macros[name]=body as-given. So `/save Work x` then `/save work y`
    # used to make TWO entries — and `/run work` hit the wrong one. save_macro must resolve an
    # existing case-insensitive key and OVERWRITE it (mirror create/_resolve_name): exactly
    # ONE macro survives, and a case-insensitive /run returns the LATEST body.
    store = JsonSessionStore(tmp_path / "state.json")
    store.save_macro(1, "Work", "x")
    store.save_macro(1, "work", "y")  # same name, different case → overwrite, not a 2nd entry
    macros = store.list_macros(1)
    assert len(macros) == 1, f"expected exactly one macro, got {macros!r}"
    # The stored (original-case) key is preserved; the body is the latest. /run resolves it.
    assert list(macros) == ["Work"]
    assert store.get_macro(1, "work") == "y"
    assert store.get_macro(1, "WORK") == "y"


def test_remove_macro(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.save_macro(1, "a", "x")
    assert store.remove_macro(1, "A") is True  # case-insensitive
    assert store.get_macro(1, "a") is None
    assert store.remove_macro(1, "a") is False  # already gone → clean False


def test_get_list_remove_macro_no_chat_is_safe(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    assert store.get_macro(1, "x") is None
    assert store.list_macros(1) == {}
    assert store.remove_macro(1, "x") is False


def test_save_macro_writes_0600(tmp_path):
    import os
    import stat

    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.save_macro(1, "a", "x")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_macros_independent_of_projects(tmp_path):
    """Macros live on the chat, separate from the project registry (don't collide)."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.save_macro(1, "alpha", "a macro named the same as a project")
    # The project and the macro coexist; neither clobbers the other.
    assert store.get_project(1, "alpha") is not None
    assert store.get_macro(1, "alpha") == "a macro named the same as a project"
    assert "alpha" in store.list_projects(1)


# ---- fork_pending marker (P11 T2 / B2+B3 — persisted adopt-not-yet-resumed) ----------------


def test_fork_pending_set_get_clear_roundtrip(tmp_path):
    """set_fork_pending persists the marker; get_fork_pending reads it; clearing removes it."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "adopted", "/work/x", make_active=True)
    assert store.get_fork_pending(1, "adopted") is False  # default: not pending
    store.set_fork_pending(1, "adopted", True)
    assert store.get_fork_pending(1, "adopted") is True
    # Survives a fresh store instance over the SAME file (persisted — the B2 restart property).
    assert JsonSessionStore(tmp_path / "s.json").get_fork_pending(1, "adopted") is True
    store.set_fork_pending(1, "adopted", False)
    assert store.get_fork_pending(1, "adopted") is False
    # Cleared field is removed entirely (clean record), not left as False.
    assert "fork_pending" not in store.get_project(1, "adopted")


def test_fork_pending_case_insensitive_and_unknown_raises(tmp_path):
    """set targets a named project case-insensitively (like set_session_id); unknown → raises."""
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(1, "Work", "/work", make_active=True)
    store.set_fork_pending(1, "work", True)  # case-insensitive match
    assert store.get_fork_pending(1, "WORK") is True
    with pytest.raises(UnknownProject):
        store.set_fork_pending(1, "nope", True)


def test_get_fork_pending_unknown_project_is_false(tmp_path):
    """get_fork_pending on an unknown project / chat reads False (RB1 — ordinary continue)."""
    store = JsonSessionStore(tmp_path / "s.json")
    assert store.get_fork_pending(1, "nope") is False
    store.create(1, "p", "/w", make_active=True)
    assert store.get_fork_pending(2, "p") is False  # wrong chat


# ---- proactive schedules (P14 T2) ------------------------------------------
#
# Schedule DEFINITIONS persist alongside macros/projects (atomic + 0600). The key
# RB3/RB6 property: rearm_all_schedules re-arms next_run from NOW (no missed-fire replay).

from claude_tg.scheduler import Schedule  # noqa: E402
from claude_tg.session_store import MaxSchedulesExceeded  # noqa: E402


def _sched(name="ci", **kw) -> Schedule:
    base = dict(
        name=name,
        interval_seconds=3600,
        prompt="run tests",
        chat_id=7,
        next_run=1000.0,
        project="proj",
        created_at=500.0,
    )
    base.update(kw)
    return Schedule(**base)


def test_schedule_round_trip(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    store.add_schedule(_sched())
    got = store.list_schedules(7)
    assert len(got) == 1
    s = got[0]
    # The stored next_run round-trips faithfully (the runtime re-arms it; the store doesn't).
    assert (s.name, s.interval_seconds, s.prompt, s.chat_id, s.next_run, s.project, s.paused, s.created_at) == (
        "ci", 3600, "run tests", 7, 1000.0, "proj", False, 500.0,
    )


def test_schedule_persisted_with_0600_perms(tmp_path):
    import os
    import stat

    p = tmp_path / "s.json"
    store = JsonSessionStore(p)
    store.add_schedule(_sched())
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_schedule_get_and_remove_case_insensitive(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    store.add_schedule(_sched(name="CI"))
    assert store.get_schedule(7, "ci").interval_seconds == 3600  # case-insensitive
    assert store.remove_schedule(7, "ci") is True
    assert store.list_schedules(7) == []
    # Removing a now-absent schedule is a clean False (RB1), never a raise.
    assert store.remove_schedule(7, "ci") is False


def test_schedule_overwrite_keeps_one_entry(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    store.add_schedule(_sched(name="Work", interval_seconds=3600))
    store.add_schedule(_sched(name="work", interval_seconds=7200))  # case-collide → overwrite
    got = store.list_schedules(7)
    assert len(got) == 1, "a re-create under a different case must not fork a duplicate"
    assert got[0].interval_seconds == 7200


def test_schedule_pause_resume_preserves_next_run(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    store.add_schedule(_sched(next_run=1234.0))
    assert store.set_schedule_paused(7, "ci", True) is True
    s = store.get_schedule(7, "ci")
    assert s.paused is True and s.next_run == 1234.0  # next_run untouched by pause
    assert store.set_schedule_paused(7, "ci", False) is True
    assert store.get_schedule(7, "ci").paused is False
    # Pausing an unknown schedule is a clean False (RB1).
    assert store.set_schedule_paused(7, "nope", True) is False


def test_schedule_rearm_from_now_no_missed_fire_replay(tmp_path):
    # The KEY RB3/RB6 test: a schedule whose stored next_run is far in the PAST (the bot was
    # down past its fire window) must, on re-arm, be set to now + interval — NOT replayed.
    store = JsonSessionStore(tmp_path / "s.json")
    store.add_schedule(_sched(interval_seconds=3600, next_run=1.0, paused=False))
    store.add_schedule(_sched(name="nightly", interval_seconds=86400, next_run=2.0, paused=True))
    store.rearm_all_schedules(now=10_000.0)
    a = store.get_schedule(7, "ci")
    assert a.next_run == 10_000.0 + 3600, "next_run must be re-armed to now + interval"
    b = store.get_schedule(7, "nightly")
    # Paused schedules are re-armed too (so a later /resume resumes on a fresh cadence) and the
    # paused flag is preserved.
    assert b.next_run == 10_000.0 + 86400
    assert b.paused is True


def test_schedule_rearm_persists_across_reload(tmp_path):
    # Re-arm + a FRESH store over the same file: the re-armed next_run is durable (RB6).
    p = tmp_path / "s.json"
    store = JsonSessionStore(p)
    store.add_schedule(_sched(interval_seconds=3600, next_run=1.0))
    store.rearm_all_schedules(now=50_000.0)
    reloaded = JsonSessionStore(p)
    assert reloaded.get_schedule(7, "ci").next_run == 50_000.0 + 3600


def test_schedule_max_per_chat_fail_closed(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    for i in range(3):
        store.add_schedule(_sched(name=f"s{i}"), max_per_chat=3)
    with pytest.raises(MaxSchedulesExceeded):
        store.add_schedule(_sched(name="s3"), max_per_chat=3)
    assert len(store.list_schedules(7)) == 3, "the over-cap create must NOT have been stored"


def test_schedule_overwrite_does_not_trip_cap(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    for i in range(3):
        store.add_schedule(_sched(name=f"s{i}"), max_per_chat=3)
    # Overwriting an EXISTING name (case-insensitive) does not grow the count → allowed at cap.
    store.add_schedule(_sched(name="S0", interval_seconds=120), max_per_chat=3)
    assert len(store.list_schedules(7)) == 3
    assert store.get_schedule(7, "s0").interval_seconds == 120


def test_schedule_missing_store_reads_empty_never_raises(tmp_path):
    store = JsonSessionStore(tmp_path / "absent.json")
    assert store.list_schedules(1) == []
    assert store.get_schedule(1, "x") is None
    assert store.remove_schedule(1, "x") is False
    assert store.set_schedule_paused(1, "x", True) is False
    store.rearm_all_schedules(now=0.0)  # no-op, no raise


def test_schedule_corrupt_record_skipped(tmp_path):
    p = tmp_path / "s.json"
    store = JsonSessionStore(p)
    store.add_schedule(_sched(name="good"))
    raw = json.loads(p.read_text())
    # A hand-edited record with no interval_seconds is unrenderable → skipped by list (RB1).
    raw["chats"]["7"]["schedules"]["bad"] = {"prompt": "x"}
    p.write_text(json.dumps(raw))
    names = [s.name for s in store.list_schedules(7)]
    assert "good" in names and "bad" not in names


def test_schedules_are_per_chat(tmp_path):
    store = JsonSessionStore(tmp_path / "s.json")
    store.add_schedule(_sched(name="a", chat_id=7))
    store.add_schedule(_sched(name="b", chat_id=8))
    assert [s.name for s in store.list_schedules(7)] == ["a"]
    assert [s.name for s in store.list_schedules(8)] == ["b"]


def test_schedules_coexist_with_macros_and_projects(tmp_path):
    # A schedule write must not disturb the chat's macros/projects (separate namespaces).
    store = JsonSessionStore(tmp_path / "s.json")
    store.create(7, "proj", "/work", make_active=True)
    store.save_macro(7, "m", "body")
    store.add_schedule(_sched(name="ci", chat_id=7))
    assert store.get_active(7) == "proj"
    assert store.get_macro(7, "m") == "body"
    assert [s.name for s in store.list_schedules(7)] == ["ci"]
