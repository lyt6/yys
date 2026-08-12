import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from data_model import normalize_equipment_item
from storage import (
    InstanceLock,
    SQLiteStore,
    canonical_target_url,
    target_key_for_url,
)

URL = (
    "https://yys.cbg.163.com/cgi/mweb/pl?tfid=f_kingkong&view_loc=equip_list"
    "&refer_sn=tracking-value"
)


def make_result(items, *, cycle=1, mode="full", success=True):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "success": success,
        "auth_state": "authenticated" if success else "navigation_error",
        "scan_mode": mode,
        "cycle": cycle,
        "started_at": now,
        "fetched_at": now,
        "pages_scanned": 2,
        "scan_complete": True,
        "termination_reason": "api_end",
        "observed_equip_list": items,
        "equip_list": items,
    }


def item(item_id, price="100", name="项目"):
    return normalize_equipment_item(
        {"equip_id": item_id, "equip_name": name, "price": price}, source="api"
    )


def test_tracking_parameter_does_not_change_target_key():
    without_tracking = "https://yys.cbg.163.com/cgi/mweb/pl?view_loc=equip_list&tfid=f_kingkong"
    assert target_key_for_url(URL) == target_key_for_url(without_tracking)
    assert "refer_sn" not in canonical_target_url(URL)


def test_sqlite_upsert_tracks_insert_update_and_unchanged(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)

    assert store.record_result("one", target, URL, make_result([item("A")])) == {
        "inserted": 1,
        "updated": 0,
        "unchanged": 0,
    }
    assert store.record_result(
        "one", target, URL, make_result([item("A")], cycle=2, mode="incremental")
    ) == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert store.record_result(
        "one",
        target,
        URL,
        make_result([item("A", "120")], cycle=3, mode="incremental"),
    ) == {"inserted": 0, "updated": 1, "unchanged": 0}

    loaded = store.load_items("one", target)
    assert len(loaded) == 1
    assert loaded[0]["price"] == "120"
    assert loaded[0]["seen_count"] == 3
    assert loaded[0]["first_seen_at"] <= loaded[0]["last_changed_at"]


def test_sqlite_never_deletes_items_not_seen_in_later_run(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)
    store.record_result("one", target, URL, make_result([item("A"), item("B")]))
    store.record_result(
        "one",
        target,
        URL,
        make_result([item("A", "200")], cycle=2, mode="incremental"),
    )
    assert {entry["id"] for entry in store.load_items("one", target)} == {"A", "B"}


def test_failed_run_is_recorded_without_mutating_listings(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)
    store.record_result("one", target, URL, make_result([item("A")]))
    failed = make_result([], cycle=2, success=False)
    failed["error"] = "temporary"
    store.record_result("one", target, URL, failed)

    assert len(store.load_items("one", target)) == 1
    runs = store.list_runs(account_key="one", target_key=target)
    assert runs[0]["success"] == 0
    assert runs[0]["error"] == "temporary"


def test_checkpoint_survives_new_store_instance(tmp_path):
    path = tmp_path / "cbg.sqlite3"
    target = target_key_for_url(URL)
    SQLiteStore(str(path)).record_result("one", target, URL, make_result([item("A")], cycle=7))
    checkpoint = SQLiteStore(str(path)).get_checkpoint("one", target)
    assert checkpoint["last_cycle"] == 7
    assert checkpoint["last_full_scan_at"]


def test_scan_lifecycle_finishes_existing_run_without_duplicate_history(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)
    started = datetime.now(timezone.utc).isoformat()
    run_id = store.start_scan("one", target, URL, 4, "incremental", started)
    assert store.list_runs(account_key="one")[0]["run_status"] == "running"
    assert store.get_checkpoint("one", target)["last_cycle"] == 4

    result = make_result([item("A")], cycle=4, mode="incremental")
    store.record_result("one", target, URL, result, run_id=run_id)
    runs = store.list_runs(account_key="one")
    assert len(runs) == 1
    assert runs[0]["run_status"] == "finished"
    assert runs[0]["success"] == 1
    assert runs[0]["cycle"] == 4


def test_interrupted_scan_is_recovered_and_cycle_is_not_reused(tmp_path):
    path = tmp_path / "cbg.sqlite3"
    target = target_key_for_url(URL)
    store = SQLiteStore(str(path))
    store.start_scan("one", target, URL, 8, "full")

    restarted = SQLiteStore(str(path))
    assert restarted.recover_interrupted_runs("one", target) == 1
    run = restarted.list_runs(account_key="one")[0]
    assert run["run_status"] == "interrupted"
    assert run["auth_state"] == "interrupted"
    assert run["termination_reason"] == "process_interrupted"
    assert restarted.get_checkpoint("one", target)["last_cycle"] == 8


def test_restart_delay_uses_poll_interval_and_longer_terminal_cooldown(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    failed = make_result([], cycle=1, success=False)
    failed.update(
        {
            "auth_state": "rate_limited",
            "started_at": (now - timedelta(seconds=101)).isoformat(),
            "fetched_at": (now - timedelta(seconds=100)).isoformat(),
        }
    )
    store.record_result("one", target, URL, failed)
    decision = store.get_restart_delay(
        "one",
        target,
        poll_interval_seconds=60,
        terminal_cooldown_seconds=900,
        now=now,
    )
    assert decision["reason"] == "terminal_cooldown"
    assert decision["delay_seconds"] == pytest.approx(800)

    store.record_manual_verification("one", target, URL, (now - timedelta(seconds=10)).isoformat())
    decision = store.get_restart_delay(
        "one",
        target,
        poll_interval_seconds=60,
        terminal_cooldown_seconds=900,
        now=now,
    )
    assert decision["reason"] == "poll_interval"
    assert decision["delay_seconds"] == pytest.approx(50)


def test_login_required_restart_uses_normal_poll_interval(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    failed = make_result([], cycle=1, success=False)
    failed.update(
        {
            "auth_state": "login_required",
            "started_at": (now - timedelta(seconds=11)).isoformat(),
            "fetched_at": (now - timedelta(seconds=10)).isoformat(),
        }
    )
    store.record_result("one", target, URL, failed)
    decision = store.get_restart_delay(
        "one",
        target,
        poll_interval_seconds=60,
        terminal_cooldown_seconds=900,
        now=now,
    )
    assert decision["reason"] == "poll_interval"
    assert decision["delay_seconds"] == pytest.approx(50)


def test_query_summary_options_and_runs(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)
    store.record_result(
        "one",
        target,
        URL,
        make_result([item("A", name="青行灯"), item("B", name="大天狗")]),
    )

    page = store.list_items(account_key="one", query="青行", limit=20)
    assert page["total"] == 1
    assert page["items"][0]["name"] == "青行灯"
    assert store.get_summary("one", target)["total"] == 2
    assert store.get_options()["scopes"][0]["item_count"] == 2
    assert store.list_runs(account_key="one", limit=1)[0]["observed_count"] == 2


def test_summary_reports_official_total_and_preserves_it_across_partial_runs(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)
    full = make_result([item("A")])
    full.update({"reported_total": 10000, "collection_mode": "full_query"})
    store.record_result("one", target, URL, full)

    partial = make_result([item("A")], cycle=2, mode="incremental")
    partial.update({"reported_total": None, "collection_mode": "unknown"})
    store.record_result("one", target, URL, partial)

    summary = store.get_summary("one", target)
    assert summary["total"] == 1
    assert summary["reported_total"] == 10000
    assert summary["collection_mode"] == "full_query"


def test_database_uses_wal_and_foreign_keys(tmp_path):
    path = tmp_path / "cbg.sqlite3"
    SQLiteStore(str(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_instance_lock_rejects_second_worker(tmp_path):
    lock_path = str(tmp_path / "worker.lock")
    with InstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="已有抓取进程"):
            with InstanceLock(lock_path):
                pass
    with InstanceLock(lock_path):
        pass


def test_stored_detail_has_no_sensitive_fields(tmp_path):
    store = SQLiteStore(str(tmp_path / "cbg.sqlite3"))
    target = target_key_for_url(URL)
    sensitive = normalize_equipment_item(
        {
            "equip_id": "A",
            "name": "项目",
            "price": 1,
            "access_token": "secret",
            "mobile": "secret",
        },
        source="api",
    )
    store.record_result("one", target, URL, make_result([sensitive]))
    with sqlite3.connect(store.database_path) as connection:
        payload = connection.execute("SELECT detail_json FROM listings").fetchone()[0]
    assert "secret" not in payload
    assert json.loads(payload)["equip_id"] == "A"


def test_schema_v1_is_migrated_in_place(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            INSERT INTO schema_info(version) VALUES (1);
            CREATE TABLE scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_key TEXT NOT NULL,
                target_key TEXT NOT NULL,
                target_url TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                scan_mode TEXT NOT NULL,
                success INTEGER NOT NULL,
                auth_state TEXT NOT NULL DEFAULT '',
                termination_reason TEXT NOT NULL DEFAULT '',
                coverage_complete INTEGER NOT NULL DEFAULT 0,
                pages_scanned INTEGER NOT NULL DEFAULT 0,
                observed_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );
            """
        )

    SQLiteStore(str(path))
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version FROM schema_info").fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(scan_runs)").fetchall()
        }
    assert version == 2
    assert {"cycle", "run_status"} <= columns


def test_concurrent_store_initialization_keeps_single_schema_row(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _index: SQLiteStore(str(path)), range(8)))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_info").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 2


def test_store_methods_release_database_file_handles(tmp_path):
    path = tmp_path / "handles.sqlite3"
    store = SQLiteStore(str(path))
    store.get_summary()
    store.list_runs()
    moved = tmp_path / "moved.sqlite3"
    path.rename(moved)
    assert moved.is_file()
