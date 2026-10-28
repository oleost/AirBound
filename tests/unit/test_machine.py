"""Settings restored from a backup: noticed at startup, nothing starts before the person confirms."""

from pathlib import Path

import pytest

from airbound import hosted, machine
from airbound.relay import explain
from airbound.settings import Store
from airbound.steps import landing

CONFIG_YAML = Path(__file__).resolve().parents[2] / "airbound" / "config.yaml"


def store_with(tmp_path, **settings) -> Store:
    store = Store(tmp_path / "settings.json")
    store.update(**settings)
    return store


def test_a_fresh_install_gets_a_machine_key_without_questions(tmp_path):
    store = store_with(tmp_path)
    machine.check(store, tmp_path / "machine_key")
    assert store.settings.machine_known and not store.settings.restored
    assert len(machine.key(tmp_path / "machine_key")) >= 32


def test_settings_from_before_machine_keys_are_not_taken_for_a_restore(tmp_path):
    store = store_with(tmp_path, mode="hosted", live=True)
    machine.check(store, tmp_path / "machine_key")
    assert store.settings.machine_known and not store.settings.restored


def test_settings_back_without_their_machine_key_are_a_restore(tmp_path):
    key_file = tmp_path / "machine_key"
    store = store_with(tmp_path, mode="direct", live=True)
    machine.check(store, key_file)
    old_key = machine.key(key_file)
    machine.check(store, key_file)  # a normal restart
    assert not store.settings.restored
    key_file.unlink()  # Home Assistant restored a backup, which leaves the key out
    machine.check(store, key_file)
    assert store.settings.restored
    assert machine.key(key_file) != old_key


def test_a_restore_before_anything_was_set_up_needs_no_confirmation(tmp_path):
    key_file = tmp_path / "machine_key"
    store = store_with(tmp_path)
    machine.check(store, key_file)
    key_file.unlink()
    machine.check(store, key_file)
    assert not store.settings.restored


def test_backups_leave_the_machine_key_out():
    config = CONFIG_YAML.read_text(encoding="utf-8")
    excluded = config.split("backup_exclude:\n", 1)[1].split("\n# ", 1)[0]
    assert f"  - {machine.MACHINE_FILE.name}\n" in excluded
    assert "  - frpc.toml\n" in excluded


def test_the_panel_opens_on_the_restored_page(tmp_path):
    store = store_with(tmp_path, mode="hosted", live=True, restored=True)
    assert landing(store.settings, []) == "restored"


async def test_the_relay_refusing_this_machine_is_its_own_state(monkeypatch):
    async def call(_session, method, _path, token="", body=None, machine=""):
        assert machine == "this-machine"
        return 409, {"state": "other_machine"}

    monkeypatch.setattr(hosted, "_call", call)
    hosted._accounts.clear()
    assert (await hosted.account(None, "copied-key", "this-machine"))["state"] == "other_machine"
    with pytest.raises(hosted.HostedError, match="another Home Assistant"):
        await hosted.unlink(None, "copied-key", "this-machine")


def test_other_machine_error_says_to_link_again():
    assert "Link again with Patreon" in explain("other machine: this key belongs to another Home Assistant")
