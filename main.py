"""Entrypoint for the browser collector and SQLite persistence layer."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

from cbg_fetcher import CBGFetcher, export_to_csv, export_to_json, format_equip_list
from storage import InstanceLock, SQLiteStore, safe_account_key, target_key_for_url, utc_now

DEFAULT_TARGET_URL = "https://yys.cbg.163.com/cgi/mweb/pl?view_loc=equip_list&tfid=f_kingkong"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        print(f"[配置] {name} 不是有效整数，使用默认值 {default}。", flush=True)
        return default


def load_account_configuration() -> tuple[list[dict[str, Any]], int]:
    """Load the ignored local credentials module only when the app starts."""
    try:
        module = importlib.import_module("account_pool")
    except ModuleNotFoundError as exc:
        raise ValueError(
            "缺少 account_pool.py，请复制 account_pool.example.py 后填写本地账号"
        ) from exc
    pool = getattr(module, "ACCOUNT_POOL", None)
    index = getattr(module, "ACTIVE_ACCOUNT_INDEX", 0)
    return pool, int(index)


def account_output_dir(account: dict[str, Any]) -> str:
    configured = str(account.get("output_dir") or "").strip()
    if configured:
        return configured
    return os.path.join("data", safe_account_key(account.get("name") or "account"))


def select_account(account_pool: list[dict[str, Any]], active_index: int) -> dict[str, Any]:
    if not isinstance(account_pool, list) or not account_pool:
        raise ValueError("ACCOUNT_POOL 不能为空")

    enabled = [item for item in account_pool if item.get("enabled", True)]
    account_keys = [safe_account_key(item.get("name") or "account") for item in enabled]
    if len(account_keys) != len(set(account_keys)):
        raise ValueError("启用账号的 name 规范化后不能重复，请使用不同的英文名称")
    profile_paths = [
        os.path.normcase(os.path.abspath(str(item.get("profile_dir") or ""))) for item in enabled
    ]
    if "" in [str(item.get("profile_dir") or "") for item in enabled]:
        raise ValueError("启用账号必须配置独立 profile_dir")
    if len(profile_paths) != len(set(profile_paths)):
        raise ValueError("启用账号的 profile_dir 不能重复")

    output_paths = [os.path.normcase(os.path.abspath(account_output_dir(item))) for item in enabled]
    if len(output_paths) != len(set(output_paths)):
        raise ValueError("启用账号的 output_dir 不能重复")
    if active_index < 0 or active_index >= len(account_pool):
        raise ValueError("ACTIVE_ACCOUNT_INDEX 超出账号池范围")

    account = account_pool[active_index]
    if not account.get("enabled", True):
        raise ValueError(f"账号 {active_index} 已禁用")
    if not account.get("username") or not account.get("password"):
        raise ValueError(f"账号 {active_index} 未填写 username/password")
    if not account.get("profile_dir"):
        raise ValueError(f"账号 {active_index} 未配置独立 profile_dir")
    return account


def validate_target_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or parsed.hostname != "yys.cbg.163.com":
        raise ValueError("CBG_TARGET_URL 必须是 https://yys.cbg.163.com 页面")
    return url


def load_snapshot_items(filepath: str) -> list[dict[str, Any]]:
    """Legacy JSON reader retained only for explicit migration/testing."""
    if not os.path.exists(filepath):
        return []
    with open(filepath, encoding="utf-8") as handle:
        data = json.load(handle)
    items = data.get("equip_list", [])
    if not isinstance(items, list):
        raise ValueError("旧快照 equip_list 不是数组")
    return items


def migrate_legacy_snapshot(
    store: SQLiteStore,
    *,
    account_key: str,
    target_key: str,
    target_url: str,
    json_path: str,
) -> bool:
    """Import an existing per-account JSON snapshot exactly once."""
    if store.load_items(account_key, target_key) or not os.path.exists(json_path):
        return False
    items = load_snapshot_items(json_path)
    if not items:
        return False
    now = utc_now()
    store.record_result(
        account_key,
        target_key,
        target_url,
        {
            "success": True,
            "auth_state": "legacy_import",
            "scan_mode": "migration",
            "cycle": 0,
            "started_at": now,
            "fetched_at": now,
            "pages_scanned": 0,
            "scan_complete": False,
            "termination_reason": "legacy_json_import",
            "observed_equip_list": items,
        },
    )
    return True


def main() -> int:
    try:
        account_pool, active_index = load_account_configuration()
        account = select_account(account_pool, active_index)
        target_url = validate_target_url(
            str(account.get("target_url") or os.getenv("CBG_TARGET_URL", DEFAULT_TARGET_URL))
        )
    except (TypeError, ValueError) as exc:
        print(f"[配置错误] {exc}", file=sys.stderr, flush=True)
        return 1

    account_key = safe_account_key(str(account.get("name") or active_index))
    target_key = target_key_for_url(target_url)
    username = str(account["username"])
    password = str(account["password"])
    profile_dir = str(account["profile_dir"])
    output_dir = account_output_dir(account)
    database_path = str(
        account.get("database_path")
        or os.getenv("CBG_DATABASE_PATH", os.path.join("data", "cbg.sqlite3"))
    )
    json_path = os.path.join(output_dir, "equip_data.json")
    csv_path = os.path.join(output_dir, "equip_data.csv")
    lock_path = os.path.join("data", ".locks", f"{account_key}-{target_key}.lock")

    headless = env_bool("CBG_HEADLESS", default=False)
    run_once = env_bool("CBG_RUN_ONCE", default=False)
    interval_seconds = env_int("CBG_POLL_INTERVAL_SECONDS", 60, 30)
    max_pages = env_int("CBG_MAX_PAGES", 100, 1)
    incremental_pages = env_int("CBG_INCREMENTAL_PAGES", 20, 1)
    full_refresh_interval = env_int("CBG_FULL_REFRESH_INTERVAL_SECONDS", 3600, 300)
    scroll_delay = env_int("CBG_SCROLL_DELAY_MS", 3000, 1000)
    idle_rounds = env_int("CBG_IDLE_ROUNDS", 3, 1)

    try:
        store = SQLiteStore(database_path)
        migrated = migrate_legacy_snapshot(
            store,
            account_key=account_key,
            target_key=target_key,
            target_url=target_url,
            json_path=json_path,
        )
        initial_items = store.load_items(account_key, target_key)
        checkpoint = store.get_checkpoint(account_key, target_key)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[数据库错误] {exc}", file=sys.stderr, flush=True)
        return 1

    print("=" * 64, flush=True)
    print("网易藏宝阁自动抓取服务", flush=True)
    print(f"当前账号: {account_key}", flush=True)
    print(f"目标 URL: {target_url}", flush=True)
    print(f"浏览器模式: {'无头' if headless else '有界面'}", flush=True)
    print(f"浏览器 Profile: {profile_dir}", flush=True)
    print(f"SQLite: {store.database_path}", flush=True)
    if migrated:
        print(f"已迁移旧 JSON 快照: {json_path}", flush=True)
    print(f"已恢复持久快照: {len(initial_items)} 条", flush=True)
    print(f"运行模式: {'单次' if run_once else f'每 {interval_seconds} 秒调度'}", flush=True)
    print(
        f"增量扫描最多 {incremental_pages} 轮；"
        f"每 {full_refresh_interval} 秒深扫，最多 {max_pages} 轮",
        flush=True,
    )
    print("=" * 64, flush=True)

    fetcher = CBGFetcher(user_data_dir=profile_dir)

    def persist_result(result: dict[str, Any]) -> dict[str, Any]:
        persisted = dict(result)
        stats = store.record_result(account_key, target_key, target_url, persisted)
        persisted["storage_stats"] = stats
        persisted["database_path"] = store.database_path
        if persisted.get("success"):
            persisted["equip_list"] = store.load_items(account_key, target_key)
            persisted["snapshot_item_count"] = len(persisted["equip_list"])
            os.makedirs(output_dir, exist_ok=True)
            export_to_json(persisted, json_path)
            export_to_csv(persisted, csv_path)
        return persisted

    async def handle_result(result: dict[str, Any], cycle: int) -> None:
        persisted = await asyncio.to_thread(persist_result, result)
        print(f"\n{'=' * 24} 第 {cycle} 轮 {'=' * 24}", flush=True)
        print("\n" + format_equip_list(persisted) + "\n", flush=True)
        if persisted.get("success"):
            print(f"[数据库] {store.database_path}", flush=True)
            print(f"[导出] {json_path}", flush=True)
            print(f"[导出] {csv_path}", flush=True)
        else:
            print(f"[本轮失败] {persisted.get('error')}", flush=True)
            if persisted.get("needs_user_action"):
                print("[提示] 当前安全验证需要人工在同一 Profile 中完成。", flush=True)

    try:
        with InstanceLock(lock_path):
            result = fetcher.poll_equip_data(
                url=target_url,
                username=username,
                password=password,
                result_handler=handle_result,
                headless=headless,
                interval_seconds=interval_seconds,
                max_pages=max_pages,
                incremental_pages=incremental_pages,
                full_refresh_interval_seconds=full_refresh_interval,
                scroll_delay=scroll_delay,
                idle_rounds=idle_rounds,
                max_cycles=1 if run_once else None,
                initial_items=initial_items,
                checkpoint=checkpoint,
            )
        return 0 if result.get("success") else 2
    except KeyboardInterrupt:
        print("\n[退出] 收到 Ctrl+C，浏览器会话已关闭。", flush=True)
        return 0
    except RuntimeError as exc:
        print(f"[运行错误] {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        print(f"执行发生异常: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
