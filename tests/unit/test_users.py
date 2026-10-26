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
