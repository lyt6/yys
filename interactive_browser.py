"""Manual verification helper using the active account's exact browser profile."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any

from playwright.async_api import async_playwright

from cbg_fetcher import (
    MOBILE_AUTH_STATUS,
    CBGFetcher,
    extract_structured_items,
    get_business_status,
    is_business_success,
    is_equipment_api,
)
from data_model import sanitize_sensitive_data
from main import (
    DEFAULT_TARGET_URL,
    account_database_path,
    account_profile_path,
    load_account_configuration,
    select_account,
    validate_target_url,
)
from storage import InstanceLock, SQLiteStore, safe_account_key, target_key_for_url


async def run_interactive() -> bool:
    account_pool, active_index = load_account_configuration()
    account = select_account(account_pool, active_index)
    target_url = validate_target_url(
        str(account.get("target_url") or os.getenv("CBG_TARGET_URL", DEFAULT_TARGET_URL))
    )
    profile_dir = account_profile_path(account)
    database_path = account_database_path(account)
    account_key = safe_account_key(str(account.get("name") or active_index))
    target_key = target_key_for_url(target_url)
    lock_path = os.path.join(
        "data", ".locks", f"{account_key}-{target_key}.lock"
    )
    fetcher = CBGFetcher(user_data_dir=profile_dir)
    response_state: dict[str, Any] = {
        "mobile_auth": False,
        "data_ok": False,
        "http_status": 0,
    }

    print("=" * 64, flush=True)
    print("网易藏宝阁交互式会话验证", flush=True)
    print(f"账号: {account_key}", flush=True)
    print(f"Profile: {profile_dir}", flush=True)
    print(f"SQLite: {database_path}", flush=True)
    print("请在浏览器窗口中完成登录、短信或滑块验证。", flush=True)
    print("=" * 64, flush=True)

    with InstanceLock(lock_path):
        store = SQLiteStore(database_path)
        async with async_playwright() as playwright:
            os.makedirs(fetcher.user_data_dir, exist_ok=True)
            context = await fetcher._launch_persistent_context(playwright, headless=False)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                pending: set[asyncio.Task] = set()
                captured: list[dict[str, Any]] = []
                sequence = 0

                async def process_response(response, response_sequence: int) -> None:
                    if response.status in (401, 403, 429):
                        response_state["http_status"] = response.status
                    if response.status != 200:
                        return
                    try:
                        data = sanitize_sensitive_data(await response.json())
                    except Exception:
                        return
                    status_code = get_business_status(data)
                    if status_code == MOBILE_AUTH_STATUS:
                        response_state["mobile_auth"] = True
                    captured.append(
                        {
                            "url": response.url,
                            "sequence": response_sequence,
                            "json": data,
                        }
                    )
                    if is_business_success(data) or extract_structured_items(captured[-1:], []):
                        response_state["data_ok"] = True

                def schedule_response(response) -> None:
                    nonlocal sequence
                    if not is_equipment_api(response.url):
                        return
                    sequence += 1
                    task = asyncio.create_task(process_response(response, sequence))
                    pending.add(task)
                    task.add_done_callback(pending.discard)

                async def drain() -> None:
                    while pending:
                        await asyncio.gather(*tuple(pending), return_exceptions=True)

                page.on("response", schedule_response)
                try:
                    while True:
                        response_state.update(
                            {
                                "mobile_auth": False,
                                "data_ok": False,
                                "http_status": 0,
                            }
                        )
                        captured.clear()
                        print("\n>> 正在复查当前 Profile 的访问状态...", flush=True)
                        try:
                            await page.goto(
                                target_url,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                            await page.wait_for_timeout(2500)
                            await drain()
                        except Exception as exc:
                            print(f">> 页面加载失败: {exc}", flush=True)

                        if response_state["data_ok"] and not response_state["mobile_auth"]:
                            print(
                                ">> [成功] 装备接口已正常返回，Profile 可以用于抓取。", flush=True
                            )
                            await asyncio.to_thread(
                                store.record_manual_verification,
                                account_key,
                                target_key,
                                target_url,
                            )
                            return True
                        if response_state["http_status"]:
                            print(
                                f">> 接口返回 HTTP {response_state['http_status']}，请稍后再试。",
                                flush=True,
                            )
                        elif fetcher._is_login_page(page.url):
                            print(">> 当前 Profile 尚未登录，请在浏览器中登录。", flush=True)
                        elif response_state[
                            "mobile_auth"
                        ] or await fetcher._page_requires_mobile_auth(page):
                            print(">> 当前会话仍需要手机验证，请在浏览器中完成。", flush=True)
                        else:
                            print(">> 尚未捕获到有效装备接口响应，请检查页面状态。", flush=True)

                        answer = await asyncio.to_thread(
                            input, "完成页面操作后按 Enter 复查；输入 q 退出: "
                        )
                        if answer.strip().lower() == "q":
                            return False
                finally:
                    page.remove_listener("response", schedule_response)
                    await drain()
            finally:
                await fetcher._close_context(context)


def main() -> int:
    try:
        return 0 if asyncio.run(run_interactive()) else 1
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"[无法启动] {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
