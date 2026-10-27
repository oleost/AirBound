import json

from airbound.users import blocking, describe, enrolled_user_ids


def ha_user(name, uid, **extra):
    return {
        "id": uid,
        "name": name,
        "is_active": True,
        "system_generated": False,
        "local_only": False,
        "is_owner": False,
        "credentials": [{"type": "homeassistant"}],
        **extra,
    }


def test_enrolled_ids_from_totp_and_notify(tmp_path):
    (tmp_path / "auth_module.totp").write_text(json.dumps({"data": {"users": {"1": "secret"}}}))
    (tmp_path / "auth_module.notify").write_text(json.dumps({"data": {"users": {"2": {}}}}))
    assert enrolled_user_ids(tmp_path) == {"1", "2"}


def test_enrolled_ids_without_stores(tmp_path):
    assert enrolled_user_ids(tmp_path) == set()


def test_describe_skips_system_and_inactive_users():
    users = [
        ha_user("ola", "1", is_owner=True),
        ha_user("Supervisor", "2", system_generated=True, credentials=[]),
        ha_user("gone", "3", is_active=False),
        ha_user("kari", "4", local_only=True),
    ]
    people = describe(users, enrolled={"1"}, current_user_id="1")
    assert [p["name"] for p in people] == ["ola", "kari"]
    assert people[0] == {"id": "1", "name": "ola", "mfa": True, "local_only": False, "owner": True, "you": True}


def test_blocking():
    people = describe(
        [ha_user("ola", "1"), ha_user("kari", "2"), ha_user("bestemor", "3", local_only=True)],
        enrolled={"2"},
        current_user_id=None,
    )
    assert blocking(people) == ["ola"]


def person(id_, mfa=False, local_only=False):
    return {"id": id_, "name": id_.title(), "mfa": mfa, "local_only": local_only, "owner": False, "you": False}


def test_mfa_plan_limits_people_without_mfa_and_releases_only_its_own():
    from airbound.users import mfa_plan

    people = [person("sam"), person("alex", mfa=True), person("kim", local_only=True), person("lee", mfa=True, local_only=True)]
    limit, release = mfa_plan(people, {"lee"}, enforce=True)
    assert [p["id"] for p in limit] == ["sam"]
    assert [p["id"] for p in release] == ["lee"]  # turned MFA on; kim was limited by hand and stays
    limit, release = mfa_plan(people, {"kim"}, enforce=False)
    assert limit == [] and [p["id"] for p in release] == ["kim"]


async def test_enforce_mfa_limits_releases_and_remembers(tmp_path, monkeypatch):
    from airbound import users
    from airbound.settings import Store

    people = [person("sam"), person("lee", mfa=True, local_only=True)]
    calls = []

    async def fake_list(_supervisor, _user):
        return [dict(p) for p in people]

    async def fake_set(_supervisor, user_id, local_only):
        calls.append((user_id, local_only))

    monkeypatch.setattr(users, "list_people", fake_list)
    monkeypatch.setattr(users, "set_local_only", fake_set)
    store = Store(tmp_path / "settings.json")
    store.update(live=True, require_mfa=True, auto_limited=("lee",))

    limited, released = await users.enforce_mfa(None, store)
    assert (limited, released) == (["Sam"], ["Lee"])
    assert calls == [("sam", True), ("lee", False)]
    assert store.settings.auto_limited == ("sam",)

    # Going offline lets AirBound's own limits go again.
    people[0]["local_only"] = True
    store.update(live=False)
    calls.clear()
    assert await users.enforce_mfa(None, store) == ([], ["Sam"])
    assert calls == [("sam", False)] and store.settings.auto_limited == ()


def test_ha_bans_read_and_remove_one(tmp_path):
    from airbound.ha_bans import read_bans, remove_ban

    path = tmp_path / "ip_bans.yaml"
    path.write_text(
        "198.51.100.7:\n  banned_at: '2026-10-27T06:48:39.417339+00:00'\n"
        "'2001:db8::1':\n  banned_at: '2026-10-27T07:00:00+00:00'\n"
        "203.0.113.9:\n  banned_at: '2026-10-27T08:00:00+00:00'\n",
        encoding="utf-8",
    )
    assert [b["ip"] for b in read_bans(path)] == ["198.51.100.7", "2001:db8::1", "203.0.113.9"]
    assert read_bans(path)[0]["banned_at"].startswith("2026-10-27T06:48")
    assert remove_ban("2001:db8::1", path) is True
    assert [b["ip"] for b in read_bans(path)] == ["198.51.100.7", "203.0.113.9"]
    assert remove_ban("192.0.2.1", path) is False
    assert read_bans(tmp_path / "missing.yaml") == []
