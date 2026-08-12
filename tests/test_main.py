from pathlib import Path

import pytest

from main import (
    account_database_path,
    account_profile_path,
    select_account,
    validate_target_url,
)
from storage import PROJECT_ROOT


def account(name, profile, enabled=True):
    return {
        "name": name,
        "username": f"{name}-user",
        "password": f"{name}-password",
        "profile_dir": profile,
        "enabled": enabled,
    }


def test_account_selection_requires_isolated_profiles(tmp_path):
    pool = [
        account("one", str(tmp_path / "p1")),
        account("two", str(tmp_path / "p2")),
    ]
    assert select_account(pool, 1)["name"] == "two"
    pool[1]["profile_dir"] = pool[0]["profile_dir"]
    with pytest.raises(ValueError, match="profile_dir"):
        select_account(pool, 0)


def test_account_selection_rejects_colliding_database_keys(tmp_path):
    pool = [
        account("中文甲", str(tmp_path / "p1")),
        account("中文乙", str(tmp_path / "p2")),
    ]
    with pytest.raises(ValueError, match="name"):
        select_account(pool, 0)


def test_runtime_paths_are_project_rooted_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CBG_DATABASE_PATH", raising=False)
    profile = account_profile_path({"profile_dir": "browser_profiles/one"})
    database = account_database_path({})
    assert Path(profile) == PROJECT_ROOT / "browser_profiles" / "one"
    assert Path(database) == PROJECT_ROOT / "data" / "cbg.sqlite3"


def test_account_specific_database_path_is_project_rooted():
    assert Path(account_database_path({"database_path": "state/custom.sqlite3"})) == (
        PROJECT_ROOT / "state" / "custom.sqlite3"
    )


def test_target_validation_rejects_other_hosts_and_http():
    with pytest.raises(ValueError):
        validate_target_url("https://example.com/list")
    with pytest.raises(ValueError):
        validate_target_url("http://yys.cbg.163.com/list")
    assert validate_target_url("https://yys.cbg.163.com/list").startswith("https://")
