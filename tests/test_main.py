import json
import os

import pytest

from main import (
    account_output_dir,
    load_snapshot_items,
    migrate_legacy_snapshot,
    select_account,
    validate_target_url,
)
from storage import SQLiteStore, target_key_for_url


def account(name, profile, output, enabled=True):
    return {
        "name": name,
        "username": f"{name}-user",
        "password": f"{name}-password",
        "profile_dir": profile,
        "output_dir": output,
        "enabled": enabled,
    }


def test_account_selection_requires_isolated_profiles_and_outputs(tmp_path):
    pool = [
        account("one", str(tmp_path / "p1"), str(tmp_path / "o1")),
        account("two", str(tmp_path / "p2"), str(tmp_path / "o2")),
    ]
    assert select_account(pool, 1)["name"] == "two"
    pool[1]["profile_dir"] = pool[0]["profile_dir"]
    with pytest.raises(ValueError, match="profile_dir"):
        select_account(pool, 0)


def test_account_selection_rejects_colliding_database_keys(tmp_path):
    pool = [
        account("中文甲", str(tmp_path / "p1"), str(tmp_path / "o1")),
        account("中文乙", str(tmp_path / "p2"), str(tmp_path / "o2")),
    ]
    with pytest.raises(ValueError, match="name"):
        select_account(pool, 0)


def test_output_dir_default_is_safe():
    assert account_output_dir({"name": "临时 account 1"}) == os.path.join("data", "account_1")


def test_target_validation_rejects_other_hosts_and_http():
    with pytest.raises(ValueError):
        validate_target_url("https://example.com/list")
    with pytest.raises(ValueError):
        validate_target_url("http://yys.cbg.163.com/list")
    assert validate_target_url("https://yys.cbg.163.com/list").startswith("https://")


def test_legacy_corrupt_snapshot_is_never_silently_treated_as_empty(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_snapshot_items(str(path))


def test_legacy_account_snapshot_is_imported_once(tmp_path):
    url = "https://yys.cbg.163.com/cgi/mweb/pl?view_loc=equip_list"
    target = target_key_for_url(url)
    path = tmp_path / "equip_data.json"
    path.write_text(
        json.dumps({"equip_list": [{"id": "A", "name": "旧项目", "price": "10"}]}),
        encoding="utf-8",
    )
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))

    assert migrate_legacy_snapshot(
        store,
        account_key="one",
        target_key=target,
        target_url=url,
        json_path=str(path),
    )
    assert not migrate_legacy_snapshot(
        store,
        account_key="one",
        target_key=target,
        target_url=url,
        json_path=str(path),
    )
    assert store.load_items("one", target)[0]["id"] == "A"
