"""Browser-based NetEase CBG collector with deterministic upsert semantics."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from data_model import (
    count_equipment_changes,
    deduplicate_items,
    equipment_identity_key,
    is_sensitive_key,
    merge_equipment_snapshots,
    normalize_equipment_item,
    sanitize_sensitive_data,
)

try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    async_playwright = None
    PLAYWRIGHT_AVAILABLE = False


MOBILE_AUTH_STATUS = "MOBILE_AUTH"
SUCCESS_STATUS_CODES = {"", "OK", "SUCCESS"}
ALLOWED_API_HOSTS = {"yys.cbg.163.com"}
DATA_API_EXACT_PATHS = {
    "/cgi-bin/recommend.py",
    "/cgi-bin/query.py",
    "/cgi/api/equip",
    "/cgi/api/search",
}
DATA_API_PATH_PREFIXES = (
    "/cgi/api/equip/",
    "/cgi/api/search/",
)
EQUIPMENT_CONTAINER_KEYS = {
    "equip",
    "equip_desc",
    "equip_list",
    "equips",
    "items",
    "records",
    "rows",
    "selling_list",
}
WRAPPER_KEYS = {"data", "result"}
LOGIN_AGREEMENT_CHECKBOX_SELECTOR = ", ".join(
    (
        '.fur-agree-4 input[type="checkbox"]',
        '.u-zc-agree input[type="checkbox"]',
        'input.zc-un-login[type="checkbox"]',
        '.m-mail-clause input[type="checkbox"]',
        '.fur-agree input[type="checkbox"]',
        '.j-mail-clause-span input[type="checkbox"]',
        'input[type="checkbox"][name*="agree" i]',
        'input[type="checkbox"][id*="agree" i]',
        'input[type="checkbox"][class*="agree" i]',
        '[role="checkbox"][aria-label*="协议"]',
        '[role="checkbox"][aria-label*="条款"]',
        '[role="checkbox"][aria-label*="隐私"]',
    )
)
LOGIN_AGREEMENT_CONTROL_SELECTOR = ", ".join(
    (
        "div.fur-agree-4 > span.u-zc-agree",
        "span.u-zc-agree",
        "span.u-dl-agree",
        "span.j-mail-clause-span",
        "div.m-mail-clause > span:first-child",
        "div.fur-agree > span:first-child",
        "div.m-mail-clause",
        "div.fur-agree",
    )
)
LOGIN_AGREEMENT_ERROR_SELECTOR = ", ".join(
    (
        ".j-err",
        ".u-err",
        ".ferrorhead",
        ".error-msg",
        '[role="alert"]',
    )
)
LOGIN_AGREEMENT_ERROR_MARKERS = (
    "需要同意相关条款",
    "需要同意协议",
    "请先阅读并同意",
    "请阅读并同意",
    "请先勾选",
    "请勾选",
    "请先同意",
)
LOGIN_AGREEMENT_TEXT_MARKERS = (
    "已阅读并同意",
    "我已阅读并同意",
    "阅读并同意",
)


def parse_cbg_url(url: str) -> dict[str, str]:
    """Parse URL parameters without inventing equivalence between fields."""
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    return {key: values[0] for key, values in query_params.items() if values}


def is_equipment_api(url: str) -> bool:
    """Allow only HTTPS equipment APIs on the expected CBG host."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or parsed.hostname not in ALLOWED_API_HOSTS:
        return False
    path = parsed.path.lower()
    return path in DATA_API_EXACT_PATHS or any(
        path.startswith(prefix) for prefix in DATA_API_PATH_PREFIXES
    )


def get_business_status(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("status_code") or "").upper()


def is_business_success(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    status_code = get_business_status(data)
    status = data.get("status")
    if status_code not in SUCCESS_STATUS_CODES:
        return False
    if status is None:
        return status_code in {"OK", "SUCCESS"}
    return str(status).strip().lower() in {"1", "true", "ok", "success"}


def get_business_error(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    status_code = get_business_status(data)
    if status_code and status_code not in SUCCESS_STATUS_CODES:
        return status_code
    if "status" in data and not is_business_success(data):
        return f"STATUS_{data.get('status')}"
    return ""


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("当前线程已有事件循环，请直接调用对应的 async_* 方法")


def _looks_like_equipment(value: dict[str, Any]) -> bool:
    has_id = any(
        value.get(key) not in (None, "")
        for key in ("equip_id", "listing_id", "ordersn", "order_sn", "id", "sn")
    )
    has_name = any(value.get(key) not in (None, "") for key in ("equip_name", "name", "title"))
    has_listing_field = any(
        key in value for key in ("price", "price_desc", "level", "equip_level", "equip_desc")
    )
    return (has_id and (has_name or has_listing_field)) or (has_name and has_listing_field)


def _find_equipment_candidates(data: Any) -> list[dict[str, Any]]:
    """Find listing objects only below known result/list containers."""
    candidates: list[dict[str, Any]] = []
    seen_objects: set[int] = set()

    def add(value: Any) -> None:
        if (
            isinstance(value, dict)
            and id(value) not in seen_objects
            and _looks_like_equipment(value)
        ):
            seen_objects.add(id(value))
            candidates.append(value)

    def visit(value: Any, hinted: bool = False, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, list):
            for child in value:
                if hinted:
                    add(child)
                visit(child, hinted, depth + 1)
            return
        if not isinstance(value, dict):
            return
        if hinted:
            add(value)
        for key, child in value.items():
            normalized = str(key).lower()
            wrapper_contains_items = normalized in WRAPPER_KEYS and (
                isinstance(child, list)
                or (isinstance(child, dict) and _looks_like_equipment(child))
            )
            child_hinted = normalized in EQUIPMENT_CONTAINER_KEYS or wrapper_contains_items
            if normalized in WRAPPER_KEYS or child_hinted:
                visit(child, child_hinted, depth + 1)

    visit(data, isinstance(data, list))
    return candidates


def _payload_has_known_empty_list(data: Any) -> bool:
    found = False

    def visit(value: Any, depth: int = 0) -> None:
        nonlocal found
        if found or depth > 8 or not isinstance(value, dict):
            return
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in EQUIPMENT_CONTAINER_KEYS | WRAPPER_KEYS and child == []:
                found = True
                return
            if normalized in WRAPPER_KEYS or normalized in EQUIPMENT_CONTAINER_KEYS:
                if isinstance(child, dict):
                    visit(child, depth + 1)

    visit(data)
    return found


def _payload_indicates_end(data: Any) -> bool:
    """Return true only for an explicit pagination-end field."""
    if not isinstance(data, dict):
        return False
    false_means_end = {"has_more", "has_next", "has_next_page", "more"}
    true_means_end = {"is_end", "is_last", "last_page", "no_more"}

    def visit(value: Any, depth: int = 0) -> bool:
        if depth > 7 or not isinstance(value, dict):
            return False
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in false_means_end and child is False:
                return True
            if normalized in true_means_end and child is True:
                return True
            if normalized in WRAPPER_KEYS and visit(child, depth + 1):
                return True
        return False

    return visit(data)


def extract_structured_items(
    captured_apis: list[dict[str, Any]],
    dom_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize responses; stable IDs deduplicate independently of display text."""
    normalized: list[dict[str, Any]] = []
    ordered_apis = sorted(
        enumerate(captured_apis),
        key=lambda pair: (int(pair[1].get("sequence") or pair[0]), pair[0]),
    )
    for fallback_sequence, api in ordered_apis:
        data = api.get("json", {})
        if not isinstance(data, (dict, list)):
            continue
        sequence = int(api.get("sequence") or fallback_sequence)
        for raw in _find_equipment_candidates(data):
            normalized.append(
                normalize_equipment_item(
                    raw,
                    source=str(api.get("url") or "api"),
                    response_sequence=sequence,
                )
            )

    api_display_keys = {
        (str(item.get("name") or "").strip(), str(item.get("price") or "").strip())
        for item in normalized
    }
    for dom_item in dom_items:
        if not isinstance(dom_item, dict):
            continue
        display_key = (
            str(dom_item.get("name") or "").strip(),
            str(dom_item.get("price") or "").strip(),
        )
        if not display_key[0] or display_key in api_display_keys:
            continue
        normalized.append(normalize_equipment_item(dom_item, source="dom", response_sequence=0))
    return deduplicate_items(normalized)


def _equipment_identity(item: dict[str, Any]) -> tuple[str, str]:
    """Backward-compatible tuple wrapper used by older callers/tests."""
    identity = equipment_identity_key(item)
    return ("id" if not identity.startswith("display:") else "display", identity)


def summarize_api_payloads(
    captured_apis: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return schema-only diagnostics, never response values."""
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for api in sorted(
        captured_apis,
        key=lambda item: int(item.get("sequence") or 0),
        reverse=True,
    ):
        data = api.get("json")
        list_paths: list[str] = []

        def visit(
            value: Any,
            path: str,
            depth: int,
            paths: list[str] = list_paths,
        ) -> None:
            if depth > 5 or len(paths) >= 20:
                return
            if isinstance(value, list):
                paths.append(f"{path}[{len(value)}]")
                if value:
                    visit(value[0], f"{path}[]", depth + 1)
            elif isinstance(value, dict):
                for key, child in value.items():
                    if not is_sensitive_key(str(key)):
                        visit(child, f"{path}.{key}", depth + 1)

        visit(data, "$", 0)
        summary = {
            "endpoint": api.get("url", ""),
            "status_code": get_business_status(data),
            "top_keys": sorted(data.keys())[:30] if isinstance(data, dict) else [],
            "list_paths": list_paths,
            "root_type": type(data).__name__,
        }
        fingerprint = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        if fingerprint not in seen:
            seen.add(fingerprint)
            summaries.append(summary)
        if len(summaries) >= 5:
            break
    return summaries


class CBGFetcher:
    BASE_LOGIN_URL = "https://yys.cbg.163.com/cgi/mweb/show_login"

    def __init__(
        self,
        user_data_dir: str = "./browser_profile_stable",
        browser_channel: str | None = None,
        browser_start_attempts: int | None = None,
        browser_retry_delay_seconds: int | None = None,
    ) -> None:
        profile_path = Path(user_data_dir)
        if not profile_path.is_absolute():
            profile_path = Path(__file__).resolve().parent / profile_path
        self.user_data_dir = str(profile_path.resolve())
        configured = (
            browser_channel
            if browser_channel is not None
            else os.getenv("CBG_BROWSER_CHANNEL", "chrome")
        )
        self.browser_channel = configured.strip() or None
        self.browser_start_attempts = self._positive_setting(
            browser_start_attempts,
            os.getenv("CBG_BROWSER_START_ATTEMPTS"),
            default=3,
        )
        self.browser_retry_delay_seconds = self._positive_setting(
            browser_retry_delay_seconds,
            os.getenv("CBG_BROWSER_RETRY_DELAY_SECONDS"),
            default=3,
        )

    @staticmethod
    def _positive_setting(explicit: int | None, environment: str | None, default: int) -> int:
        try:
            return max(1, int(explicit if explicit is not None else environment or default))
        except (TypeError, ValueError):
            return default

    def _persistent_context_options(self, headless: bool) -> dict[str, Any]:
        options: dict[str, Any] = {
            "user_data_dir": self.user_data_dir,
            "headless": headless,
        }
        if self.browser_channel:
            options["channel"] = self.browser_channel
        return options

    async def _launch_persistent_context(self, playwright, headless: bool):
        """Retry transient Profile/Chrome startup failures without killing other processes."""
        last_error: Exception | None = None
        for attempt in range(1, self.browser_start_attempts + 1):
            try:
                return await playwright.chromium.launch_persistent_context(
                    **self._persistent_context_options(headless)
                )
            except Exception as exc:
                last_error = exc
                if attempt >= self.browser_start_attempts:
                    break
                delay = self.browser_retry_delay_seconds * attempt
                print(
                    f">> [浏览器] 启动失败（{attempt}/{self.browser_start_attempts}），"
                    f"{delay} 秒后重试: {exc}",
                    flush=True,
                )
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    @staticmethod
    async def _close_context(context) -> None:
        try:
            await context.close()
        except Exception as exc:
            print(f">> [浏览器] 关闭会话时出现警告: {exc}", flush=True)

    def login(
        self,
        username: str,
        password: str,
        headless: bool = False,
        timeout: int = 30000,
    ) -> bool:
        return _run_async(self.async_login(username, password, headless, timeout))

    async def async_login(
        self,
        username: str,
        password: str,
        headless: bool = False,
        timeout: int = 30000,
    ) -> bool:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright 未安装")
        async with async_playwright() as playwright:
            os.makedirs(self.user_data_dir, exist_ok=True)
            context = await self._launch_persistent_context(playwright, headless)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                return await self._do_login_in_page(page, context, username, password, timeout)
            finally:
                await self._close_context(context)

    @staticmethod
    async def _agreement_control_reports_checked(control) -> bool:
        try:
            if await control.get_attribute("aria-checked") == "true":
                return True
            if await control.get_attribute("data-checked") == "true":
                return True
            class_name = str(await control.get_attribute("class") or "").lower()
            if any(
                marker in class_name
                for marker in ("checked", "selected", "is-active", "is-on", "agree-ok")
            ):
                return True
        except Exception:
            pass
        try:
            return bool(
                await control.evaluate(
                    """
                    element => {
                        const nodes = [element, ...element.querySelectorAll('*')];
                        return nodes.some(node => {
                            if (node instanceof HTMLInputElement && node.type === 'checkbox') {
                                return node.checked && !node.indeterminate;
                            }
                            const ariaChecked = node.getAttribute?.('aria-checked');
                            const dataChecked = node.getAttribute?.('data-checked');
                            const className = String(node.className || '').toLowerCase();
                            return ariaChecked === 'true'
                                || dataChecked === 'true'
                                || /(^|[\\s_-])(checked|selected|active|on)([\\s_-]|$)/.test(
                                    className
                                );
                        });
                    }
                    """
                )
            )
        except Exception:
            return False

    async def _set_agreement_checkbox(self, checkbox, *, force: bool = False) -> bool:
        """Toggle through the visible square first so NetEase's UI handler also runs."""
        try:
            is_native = await checkbox.get_attribute("type") == "checkbox"
            if not is_native:
                if await checkbox.get_attribute("aria-checked") != "true":
                    await checkbox.click(force=force, timeout=2000)
                return await checkbox.get_attribute("aria-checked") == "true"

            if await checkbox.is_checked(timeout=500):
                return True

            # The current email form renders the visible square on the parent
            # span (`u-zc-agree`) and keeps the native input inside it. Clicking
            # that span is important: force-checking only the input can leave the
            # component's visual/application state unchanged.
            try:
                visual_toggle = checkbox.locator("xpath=..")
                if await visual_toggle.is_visible(timeout=500):
                    await visual_toggle.click(force=force, timeout=2000)
            except Exception:
                pass
            if await checkbox.is_checked(timeout=500):
                return True

            try:
                await checkbox.check(force=force, timeout=2000)
            except Exception:
                if force:
                    raise
                await checkbox.check(force=True, timeout=2000)
            return await checkbox.is_checked(timeout=500)
        except Exception:
            return False

    async def _check_agreement_next_to_text(
        self,
        login_frame,
        *,
        force: bool = False,
    ) -> tuple[bool, bool]:
        """Find the checkbox in the same visible row as the agreement wording."""
        found = False
        for marker in LOGIN_AGREEMENT_TEXT_MARKERS:
            try:
                labels = login_frame.get_by_text(marker, exact=False)
                label_count = await labels.count()
            except Exception:
                continue
            for index in range(label_count):
                label = labels.nth(index)
                try:
                    if not await label.is_visible(timeout=500):
                        continue
                    found = True
                    row = label.locator(
                        "xpath=ancestor-or-self::*[.//input[@type='checkbox'] "
                        "or .//*[@role='checkbox']][1]"
                    )
                    if not await row.count():
                        continue
                    checkboxes = row.locator(
                        'input[type="checkbox"], [role="checkbox"]'
                    )
                    for checkbox_index in range(await checkboxes.count()):
                        if await self._set_agreement_checkbox(
                            checkboxes.nth(checkbox_index),
                            force=force,
                        ):
                            return True, True
                except Exception:
                    continue
        return found, False

    async def _ensure_login_agreement(self, login_frame, *, force: bool = False) -> bool:
        """Click the visible agreement square and verify its underlying state."""
        found, checked = await self._check_agreement_next_to_text(
            login_frame,
            force=force,
        )
        if checked:
            print(">> [登录] 已点击可见方框并校验登录协议。", flush=True)
            return True

        checkbox_count = 0
        try:
            checkboxes = login_frame.locator(LOGIN_AGREEMENT_CHECKBOX_SELECTOR)
            checkbox_count = await checkboxes.count()
        except Exception:
            checkboxes = None

        try:
            controls = login_frame.locator(LOGIN_AGREEMENT_CONTROL_SELECTOR)
            control_count = await controls.count()
        except Exception:
            controls = None
            control_count = 0

        for index in range(control_count):
            control = controls.nth(index)
            try:
                if not await control.is_visible(timeout=500):
                    continue
                found = True
                if await self._agreement_control_reports_checked(control):
                    print(">> [登录] 登录协议已处于勾选状态。", flush=True)
                    return True
                try:
                    nested = control.locator(
                        'input[type="checkbox"], [role="checkbox"]'
                    )
                    for checkbox_index in range(await nested.count()):
                        if await self._set_agreement_checkbox(
                            nested.nth(checkbox_index),
                            force=force,
                        ):
                            print(">> [登录] 已点击可见方框并校验登录协议。", flush=True)
                            return True
                except Exception:
                    pass
                await control.click(force=force, timeout=2000)
                if await self._agreement_control_reports_checked(control):
                    print(">> [登录] 已勾选并校验登录协议。", flush=True)
                else:
                    print(">> [登录] 已点击登录协议控件，将在提交时再次校验。", flush=True)
                return True
            except Exception:
                continue

        checked_count = 0
        for index in range(checkbox_count):
            found = True
            if await self._set_agreement_checkbox(checkboxes.nth(index), force=force):
                checked_count += 1
        if checkbox_count and checked_count == checkbox_count:
            print(">> [登录] 已勾选并校验登录协议。", flush=True)
            return True

        if not found:
            print(">> [登录] 当前登录表单未显示协议勾选控件。", flush=True)
            return True
        print(">> [登录] 协议控件存在，但无法完成勾选。", flush=True)
        return False

    @staticmethod
    async def _login_agreement_error_visible(login_frame) -> bool:
        try:
            errors = login_frame.locator(LOGIN_AGREEMENT_ERROR_SELECTOR)
            count = await errors.count()
        except Exception:
            errors = None
            count = 0
        for index in range(count):
            error = errors.nth(index)
            try:
                if not await error.is_visible(timeout=300):
                    continue
                message = str(await error.inner_text(timeout=500) or "")
                if any(marker in message for marker in LOGIN_AGREEMENT_ERROR_MARKERS):
                    return True
            except Exception:
                continue
        # The current email form renders this warning as ordinary red text rather
        # than consistently using one of the historical error classes.
        for marker in LOGIN_AGREEMENT_ERROR_MARKERS:
            try:
                messages = login_frame.get_by_text(marker, exact=False)
                for index in range(await messages.count()):
                    if await messages.nth(index).is_visible(timeout=300):
                        return True
            except Exception:
                continue
        return False

    async def _do_login_in_page(
        self,
        page,
        context,
        username: str,
        password: str,
        timeout: int = 30000,
    ) -> bool:
        target_url = f"{self.BASE_LOGIN_URL}?back_url=%2Fcgi%2Fmweb%2Fpl"
        print(">> [登录] 正在加载网易藏宝阁登录页面...", flush=True)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(1200)

        login_frame = next(
            (frame for frame in page.frames if "reg.163.com" in frame.url.lower()),
            page,
        )
        try:
            account_tab = login_frame.locator("div.u-head1").first
            if await account_tab.is_visible(timeout=1500):
                await account_tab.click()
        except Exception:
            pass

        username_input = login_frame.locator('input[name="email"], input.dlemail, #phoneipt').first
        password_input = login_frame.locator('input[name="password"], input.dlpwd').first
        if not await username_input.is_visible(timeout=5000):
            print(">> [登录] 未找到账号输入框，登录页结构可能已变化。", flush=True)
            return False
        if not await password_input.is_visible(timeout=5000):
            print(">> [登录] 未找到密码输入框，登录页结构可能已变化。", flush=True)
            return False

        print(">> [登录] 正在填写登录凭据...", flush=True)
        await username_input.fill(username)
        await password_input.fill(password)
        if not await self._ensure_login_agreement(login_frame):
            return False

        login_button = login_frame.locator("a.u-loginbtn, div.loginbox a").first
        if not await login_button.is_visible(timeout=5000):
            print(">> [登录] 未找到登录按钮。", flush=True)
            return False
        for submit_attempt in range(2):
            print(">> [登录] 正在提交登录...", flush=True)
            await login_button.click()
            await page.wait_for_timeout(600)
            if not await self._login_agreement_error_visible(login_frame):
                break
            if submit_attempt == 1:
                print(">> [登录] 登录页仍提示必须同意协议，已停止本次提交。", flush=True)
                return False
            print(">> [登录] 登录页提示协议未勾选，正在强制重试一次。", flush=True)
            if not await self._ensure_login_agreement(login_frame, force=True):
                return False
        try:
            await page.wait_for_url(lambda current: not self._is_login_page(current), timeout=10000)
        except Exception:
            await page.wait_for_timeout(1500)

        cookie_names = {cookie["name"] for cookie in await context.cookies()}
        cookie_candidate = bool(
            cookie_names.intersection({"sid", "cbg_sid", "NTES_SESS", "S_INFO"})
        )
        if cookie_candidate:
            print(">> [登录] 已建立候选登录态，正在验证装备接口...", flush=True)
        else:
            print(">> [登录] 登录表单已提交，正在由目标接口验证结果...", flush=True)
        # Cookie 名称不是稳定协议。提交成功后始终转到目标页，由目标页面、
        # 手机验证状态和装备接口响应共同给出最终认证结论。
        return True

    @staticmethod
    def _failure_result(
        url: str,
        error: str,
        auth_state: str,
        needs_user_action: bool = False,
    ) -> dict[str, Any]:
        params = parse_cbg_url(url)
        return {
            "success": False,
            "error": error,
            "auth_state": auth_state,
            "needs_user_action": needs_user_action,
            "refer_sn": params.get("refer_sn", ""),
            "params": params,
            "equip_list": [],
            "observed_equip_list": [],
            "captured_apis": [],
        }

    def fetch_equip_data(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        headless: bool = False,
        max_pages: int = 100,
        scroll_delay: int = 3000,
        idle_rounds: int = 3,
    ) -> dict[str, Any]:
        return _run_async(
            self.async_fetch_equip_data(
                url, username, password, headless, max_pages, scroll_delay, idle_rounds
            )
        )

    async def async_fetch_equip_data(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        headless: bool = False,
        max_pages: int = 100,
        scroll_delay: int = 3000,
        idle_rounds: int = 3,
    ) -> dict[str, Any]:
        if not PLAYWRIGHT_AVAILABLE:
            return self._failure_result(
                url, "Playwright 未安装，无法启动浏览器。", "browser_unavailable"
            )
        async with async_playwright() as playwright:
            os.makedirs(self.user_data_dir, exist_ok=True)
            try:
                context = await self._launch_persistent_context(playwright, headless)
            except Exception as exc:
                return self._failure_result(url, f"浏览器启动失败: {exc}", "browser_unavailable")
            try:
                return await self._async_fetch_in_context(
                    context, url, username, password, max_pages, scroll_delay, idle_rounds, []
                )
            finally:
                await self._close_context(context)

    def poll_equip_data(
        self,
        url: str,
        username: str | None,
        password: str | None,
        result_handler,
        headless: bool = False,
        interval_seconds: int = 60,
        max_pages: int = 100,
        incremental_pages: int = 20,
        full_refresh_interval_seconds: int = 3600,
        scroll_delay: int = 3000,
        idle_rounds: int = 3,
        max_cycles: int | None = None,
        initial_items: list[dict[str, Any]] | None = None,
        checkpoint: dict[str, Any] | None = None,
        initial_delay_seconds: float = 0,
        cycle_started_handler=None,
    ) -> dict[str, Any]:
        return _run_async(
            self.async_poll_equip_data(
                url=url,
                username=username,
                password=password,
                result_handler=result_handler,
                headless=headless,
                interval_seconds=interval_seconds,
                max_pages=max_pages,
                incremental_pages=incremental_pages,
                full_refresh_interval_seconds=full_refresh_interval_seconds,
                scroll_delay=scroll_delay,
                idle_rounds=idle_rounds,
                max_cycles=max_cycles,
                initial_items=initial_items,
                checkpoint=checkpoint,
                initial_delay_seconds=initial_delay_seconds,
                cycle_started_handler=cycle_started_handler,
            )
        )

    async def async_poll_equip_data(
        self,
        url: str,
        username: str | None,
        password: str | None,
        result_handler,
        headless: bool = False,
        interval_seconds: int = 60,
        max_pages: int = 100,
        incremental_pages: int = 20,
        full_refresh_interval_seconds: int = 3600,
        scroll_delay: int = 3000,
        idle_rounds: int = 3,
        max_cycles: int | None = None,
        initial_items: list[dict[str, Any]] | None = None,
        checkpoint: dict[str, Any] | None = None,
        initial_delay_seconds: float = 0,
        cycle_started_handler=None,
    ) -> dict[str, Any]:
        interval_seconds = max(30, int(interval_seconds))
        max_pages = max(1, int(max_pages))
        incremental_pages = max(1, int(incremental_pages))
        full_refresh_interval_seconds = max(interval_seconds, int(full_refresh_interval_seconds))
        state = dict(checkpoint or {})
        snapshot_items = deduplicate_items(initial_items or [])
        cycle = int(state.get("last_cycle") or 0)
        full_refreshed_at = state.get("last_full_scan_at")

        full_due_in = 0.0
        if full_refreshed_at:
            try:
                previous_full = datetime.fromisoformat(str(full_refreshed_at))
                if previous_full.tzinfo is None:
                    previous_full = previous_full.replace(tzinfo=timezone.utc)
                elapsed = max(
                    0.0,
                    (datetime.now(timezone.utc) - previous_full).total_seconds(),
                )
                full_due_in = max(0.0, full_refresh_interval_seconds - elapsed)
            except ValueError:
                full_due_in = 0.0
        if state and not bool(state.get("last_full_scan_complete")):
            full_due_in = 0.0

        try:
            initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        except (TypeError, ValueError):
            initial_delay_seconds = 0.0

        async def begin_cycle(cycle_number: int, scan_mode: str, started_at: str):
            if cycle_started_handler is None:
                return None
            token = cycle_started_handler(cycle_number, scan_mode, started_at)
            return await token if inspect.isawaitable(token) else token

        stop_states = {
            "access_denied",
            "browser_unavailable",
            "business_error",
            "login_required",
            "mobile_verification_required",
            "rate_limited",
        }
        consecutive_failures = 0
        executed = 0
        next_full_at = time.monotonic() + full_due_in
        last_result = self._failure_result(url, "尚未执行抓取", "not_started")

        if initial_delay_seconds:
            print(
                f">> [重启保护] 等待 {initial_delay_seconds:.0f} 秒后再访问目标页面。",
                flush=True,
            )
            await asyncio.sleep(initial_delay_seconds)
        next_poll_at = time.monotonic()

        if not PLAYWRIGHT_AVAILABLE:
            started_at = datetime.now(timezone.utc).isoformat()
            cycle += 1
            scan_mode = "full" if time.monotonic() >= next_full_at else "incremental"
            result = self._failure_result(
                url, "Playwright 未安装，无法启动轮询服务。", "browser_unavailable"
            )
            result.update(
                {
                    "cycle": cycle,
                    "started_at": started_at,
                    "fetched_at": started_at,
                    "scan_mode": scan_mode,
                    "termination_reason": "browser_unavailable",
                }
            )
            run_id = await begin_cycle(cycle, scan_mode, started_at)
            if run_id is not None:
                result["_run_id"] = run_id
            handled = result_handler(result, cycle)
            if inspect.isawaitable(handled):
                await handled
            return result

        async with async_playwright() as playwright:
            os.makedirs(self.user_data_dir, exist_ok=True)
            try:
                context = await self._launch_persistent_context(playwright, headless)
            except Exception as exc:
                started_at = datetime.now(timezone.utc).isoformat()
                cycle += 1
                scan_mode = "full" if time.monotonic() >= next_full_at else "incremental"
                result = self._failure_result(url, f"浏览器启动失败: {exc}", "browser_unavailable")
                result.update(
                    {
                        "cycle": cycle,
                        "started_at": started_at,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "scan_mode": scan_mode,
                        "termination_reason": "browser_start_failed",
                    }
                )
                run_id = await begin_cycle(cycle, scan_mode, started_at)
                if run_id is not None:
                    result["_run_id"] = run_id
                handled = result_handler(result, cycle)
                if inspect.isawaitable(handled):
                    await handled
                return result

            try:
                while max_cycles is None or executed < max_cycles:
                    started_monotonic = time.monotonic()
                    started_at = datetime.now(timezone.utc).isoformat()
                    cycle += 1
                    executed += 1
                    is_full_refresh = started_monotonic >= next_full_at
                    scan_mode = "full" if is_full_refresh else "incremental"
                    scan_rounds = max_pages if is_full_refresh else incremental_pages
                    print(
                        f">> [轮询] 第 {cycle} 轮：{'深度扫描' if is_full_refresh else '增量扫描'}",
                        flush=True,
                    )
                    run_id = await begin_cycle(cycle, scan_mode, started_at)
                    try:
                        fetched = await self._async_fetch_in_context(
                            context,
                            url,
                            username,
                            password,
                            scan_rounds,
                            scroll_delay,
                            idle_rounds,
                            snapshot_items,
                        )
                    except Exception as exc:
                        fetched = self._failure_result(
                            url,
                            f"采集器执行异常: {exc}",
                            "collector_error",
                        )
                        fetched["termination_reason"] = "collector_exception"
                    fetched_at = datetime.now(timezone.utc).isoformat()
                    last_result = dict(fetched)
                    last_result.update(
                        {
                            "cycle": cycle,
                            "started_at": started_at,
                            "fetched_at": fetched_at,
                            "scan_mode": scan_mode,
                        }
                    )
                    if run_id is not None:
                        last_result["_run_id"] = run_id

                    if last_result.get("success"):
                        observed = deduplicate_items(last_result.get("equip_list", []))
                        last_result["observed_equip_list"] = observed
                        snapshot_items = merge_equipment_snapshots(snapshot_items, observed)
                        last_result["incremental_item_count"] = len(observed)
                        last_result["equip_list"] = list(snapshot_items)
                        last_result["snapshot_item_count"] = len(snapshot_items)
                        if is_full_refresh:
                            full_refreshed_at = fetched_at
                            next_full_at = started_monotonic + full_refresh_interval_seconds
                        last_result["full_refreshed_at"] = full_refreshed_at
                        last_result["snapshot_may_include_stale"] = bool(snapshot_items)

                    handled = result_handler(last_result, cycle)
                    if inspect.isawaitable(handled):
                        await handled

                    if last_result.get("success"):
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        if last_result.get("auth_state") in stop_states:
                            break
                        if consecutive_failures >= 3:
                            last_result["error"] = (
                                f"{last_result.get('error', '抓取失败')}；"
                                "连续失败 3 次，轮询已停止。"
                            )
                            break

                    if max_cycles is not None and executed >= max_cycles:
                        break
                    next_poll_at += interval_seconds
                    now = time.monotonic()
                    while next_poll_at <= now:
                        next_poll_at += interval_seconds
                    await asyncio.sleep(max(0.0, next_poll_at - now))
                return last_result
            finally:
                await self._close_context(context)

    async def _async_fetch_in_context(
        self,
        context,
        url: str,
        username: str | None,
        password: str | None,
        max_pages: int,
        scroll_delay: int,
        idle_rounds: int,
        known_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        params = parse_cbg_url(url)
        captured_data: list[dict[str, Any]] = []
        pending_tasks: set[asyncio.Task] = set()
        response_event = asyncio.Event()
        response_sequence = 0
        response_state: dict[str, Any] = {
            "mobile_auth": False,
            "http_status": 0,
            "last_business_error": "",
            "last_business_error_sequence": -1,
            "last_success_sequence": -1,
            "business_success_seen": False,
            "explicit_empty_seen": False,
            "explicit_api_end": False,
            "api_response_count": 0,
        }

        def failure(error: str, auth_state: str, user_action: bool = False):
            result = self._failure_result(url, error, auth_state, user_action)
            result["captured_api_count"] = len(captured_data)
            return result

        async def drain_response_tasks() -> None:
            while pending_tasks:
                await asyncio.gather(*tuple(pending_tasks), return_exceptions=True)

        async def process_response(response, sequence: int) -> None:
            try:
                if response.status in (401, 403, 429):
                    response_state["http_status"] = response.status
                if response.status != 200:
                    return
                try:
                    raw_data = await response.json()
                except Exception:
                    return
                data = sanitize_sensitive_data(raw_data)
                status_code = get_business_status(data)
                business_error = get_business_error(data)
                if status_code == MOBILE_AUTH_STATUS:
                    response_state["mobile_auth"] = True
                elif business_error:
                    response_state["last_business_error"] = business_error
                    response_state["last_business_error_sequence"] = sequence

                candidates = _find_equipment_candidates(data)
                success = is_business_success(data) or bool(candidates)
                if success:
                    response_state["business_success_seen"] = True
                    response_state["last_success_sequence"] = sequence
                known_empty = _payload_has_known_empty_list(data)
                if success and known_empty:
                    response_state["explicit_empty_seen"] = True
                if success and (candidates or known_empty) and _payload_indicates_end(data):
                    response_state["explicit_api_end"] = True

                captured_data.append(
                    {
                        "url": urlparse(response.url).path,
                        "sequence": sequence,
                        "json": data,
                    }
                )
                response_state["api_response_count"] += 1
                response_event.set()
            except Exception:
                return

        def schedule_response(response) -> None:
            nonlocal response_sequence
            if not is_equipment_api(response.url):
                return
            response_sequence += 1
            task = asyncio.create_task(process_response(response, response_sequence))
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        async def wait_for_api_activity(timeout_ms: int) -> None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(0.5, timeout_ms / 1000)
            last_sequence = response_sequence
            last_activity_at = loop.time()
            while loop.time() < deadline:
                await drain_response_tasks()
                now = loop.time()
                if response_sequence != last_sequence:
                    last_sequence = response_sequence
                    last_activity_at = now
                if response_event.is_set() and now - last_activity_at >= 0.6:
                    break
                await asyncio.sleep(min(0.1, max(0.0, deadline - now)))
            await drain_response_tasks()

        def response_failure() -> dict[str, Any] | None:
            if response_state["mobile_auth"]:
                return failure(
                    "服务端要求手机验证，自动轮询已停止。",
                    "mobile_verification_required",
                    True,
                )
            if response_state["http_status"]:
                status = int(response_state["http_status"])
                return failure(
                    f"装备接口返回 HTTP {status}，自动轮询已停止。",
                    "rate_limited" if status == 429 else "access_denied",
                    status in (401, 403),
                )
            if (
                response_state["last_business_error"]
                and response_state["last_business_error_sequence"]
                > response_state["last_success_sequence"]
            ):
                return failure(
                    f"装备接口返回业务错误 {response_state['last_business_error']}。",
                    "business_error",
                )
            return None

        page = await context.new_page()
        page.on("response", schedule_response)
        try:
            print(">> [抓取] 正在访问目标装备页面...", flush=True)
            try:
                response_event.clear()
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await wait_for_api_activity(min(max(scroll_delay, 1500), 5000))
            except Exception as exc:
                return failure(f"页面加载失败: {exc}", "navigation_error")

            if self._is_login_page(page.url):
                if not (username and password):
                    return failure(
                        "当前 Profile 未登录，且账号池未提供有效账号密码。",
                        "login_required",
                        True,
                    )
                print(">> [登录] 检测到登录页，正在自动填写账号密码...", flush=True)
                try:
                    login_candidate = await self._do_login_in_page(
                        page, context, username, password
                    )
                except Exception as exc:
                    return failure(f"自动登录执行失败: {exc}", "login_required", True)
                if not login_candidate:
                    return failure(
                        "自动登录未完成，可能需要验证码或手机验证。",
                        "login_required",
                        True,
                    )

                response_state.update(
                    {
                        "mobile_auth": False,
                        "http_status": 0,
                        "last_business_error": "",
                        "last_business_error_sequence": -1,
                        "last_success_sequence": -1,
                        "business_success_seen": False,
                        "explicit_empty_seen": False,
                        "explicit_api_end": False,
                        "api_response_count": 0,
                    }
                )
                captured_data.clear()
                try:
                    response_event.clear()
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await wait_for_api_activity(min(max(scroll_delay, 1500), 5000))
                except Exception as exc:
                    return failure(f"登录后页面加载失败: {exc}", "navigation_error")
                if self._is_login_page(page.url):
                    return failure(
                        "账号密码已提交，但目标页仍要求登录。",
                        "login_required",
                        True,
                    )

            if await self._page_requires_mobile_auth(page):
                return failure(
                    "页面要求手机验证，自动轮询已停止。",
                    "mobile_verification_required",
                    True,
                )
            state_failure = response_failure()
            if state_failure:
                return state_failure

            dom_info = await self._async_extract_dom_equip_info(page)
            observed = extract_structured_items(captured_data, dom_info.get("equip_list", []))
            pages_scanned = 1
            last_observed_count = len(observed)
            no_growth_rounds = 0
            scan_complete = bool(response_state["explicit_api_end"])
            termination_reason = "api_end" if scan_complete else "max_rounds"

            for page_number in range(2, max(1, int(max_pages)) + 1):
                if scan_complete:
                    break
                response_event.clear()
                try:
                    await self._scroll_largest_container(page)
                except Exception as exc:
                    termination_reason = f"scroll_error:{type(exc).__name__}"
                    break
                await wait_for_api_activity(max(1000, int(scroll_delay)))
                pages_scanned = page_number

                if await self._page_requires_mobile_auth(page):
                    return failure(
                        "滚动期间页面要求手机验证，自动轮询已停止。",
                        "mobile_verification_required",
                        True,
                    )
                state_failure = response_failure()
                if state_failure:
                    return state_failure

                dom_info = await self._async_extract_dom_equip_info(page)
                observed = extract_structured_items(captured_data, dom_info.get("equip_list", []))
                current_count = len(observed)
                changes = count_equipment_changes(known_items or [], observed)
                if current_count > last_observed_count:
                    no_growth_rounds = 0
                else:
                    no_growth_rounds += 1
                last_observed_count = current_count

                print(
                    f"   └─ 第 {pages_scanned} 轮：观察 {current_count} 条，"
                    f"新增/更新 {changes} 条，无新增观察 "
                    f"{no_growth_rounds}/{max(1, idle_rounds)}",
                    flush=True,
                )
                if response_state["explicit_api_end"]:
                    scan_complete = True
                    termination_reason = "api_end"
                    break
                if await self._page_has_explicit_end_marker(page):
                    scan_complete = True
                    termination_reason = "dom_end"
                    break
                if no_growth_rounds >= max(1, int(idle_rounds)):
                    termination_reason = "idle"
                    break

            await drain_response_tasks()
            dom_info = await self._async_extract_dom_equip_info(page)
            observed = extract_structured_items(captured_data, dom_info.get("equip_list", []))

            if not observed and not (
                response_state["business_success_seen"] and response_state["explicit_empty_seen"]
            ):
                result = failure(
                    "页面已打开，但没有解析到有效装备数据。",
                    "data_unavailable",
                )
                result.update(
                    {
                        "api_diagnostics": summarize_api_payloads(captured_data),
                        "captured_api_count": len(captured_data),
                        "pages_scanned": pages_scanned,
                        "termination_reason": termination_reason,
                    }
                )
                return result

            changes_detected = count_equipment_changes(known_items or [], observed)
            return {
                "success": True,
                "auth_state": "authenticated",
                "needs_user_action": False,
                "refer_sn": params.get("refer_sn", ""),
                "params": params,
                "captured_apis": captured_data,
                "captured_api_count": len(captured_data),
                "equip_list": observed,
                "observed_equip_list": observed,
                "changes_detected": changes_detected,
                "title": dom_info.get("title", ""),
                "raw_dom": dom_info.get("raw_dom", {}),
                "pages_scanned": pages_scanned,
                "scan_complete": scan_complete,
                "termination_reason": termination_reason,
                "fallback_identity_count": sum(
                    1 for item in observed if not item.get("identity_stable")
                ),
            }
        finally:
            page.remove_listener("response", schedule_response)
            await drain_response_tasks()
            await page.close()

    @staticmethod
    async def _scroll_largest_container(page) -> dict[str, Any]:
        return await page.evaluate(
            """() => {
                const root = document.scrollingElement || document.documentElement;
                const candidates = [root, ...document.querySelectorAll('body *')]
                    .filter((element, index, all) => all.indexOf(element) === index)
                    .filter(element => {
                        if (!element) return false;
                        const style = getComputedStyle(element);
                        const overflow = style.overflowY;
                        return element.scrollHeight > element.clientHeight + 16 &&
                            (element === root || overflow === 'auto' || overflow === 'scroll');
                    })
                    .sort((a, b) =>
                        (b.scrollHeight - b.clientHeight) -
                        (a.scrollHeight - a.clientHeight));
                const target = candidates[0] || root;
                const before = target === root ? window.scrollY : target.scrollTop;
                if (target === root) {
                    window.scrollTo({top: root.scrollHeight, behavior: 'auto'});
                } else {
                    target.scrollTop = target.scrollHeight;
                }
                const after = target === root ? window.scrollY : target.scrollTop;
                return {
                    moved: after > before,
                    before,
                    after,
                    scrollHeight: target.scrollHeight,
                    clientHeight: target.clientHeight
                };
            }"""
        )

    @staticmethod
    def _is_login_page(url: str) -> bool:
        lowered = url.lower()
        return "show_login" in lowered or "/login" in lowered

    @staticmethod
    async def _page_requires_mobile_auth(page) -> bool:
        lowered = page.url.lower()
        if "verify" in lowered or "uphone" in lowered:
            return True
        try:
            body = await page.locator("body").inner_text(timeout=1500)
        except Exception:
            return False
        return "验证手机" in body and "获取验证码" in body

    @staticmethod
    async def _page_has_explicit_end_marker(page) -> bool:
        try:
            body = await page.locator("body").inner_text(timeout=1200)
        except Exception:
            return False
        tail = body[-800:]
        return any(
            marker in tail for marker in ("没有更多了", "已加载全部", "已经到底了", "暂无更多")
        )

    async def _async_extract_dom_equip_info(self, page) -> dict[str, Any]:
        try:
            title = await page.title()
            items = await page.evaluate(
                """() => {
                    const results = [];
                    const cards = document.querySelectorAll(
                        '.equip_item, .equip-card, .equip-list-item, [data-equip-id]'
                    );
                    cards.forEach(card => {
                        results.push({
                            equip_id: card.dataset?.equipId || '',
                            name: card.querySelector('.name, .title')?.innerText || '',
                            price: card.querySelector('.price, .amount')?.innerText || '',
                            desc: card.innerText || ''
                        });
                    });
                    return results;
                }"""
            )
            return {
                "title": title,
                "equip_list": items,
                "raw_dom": {"item_count": len(items)},
            }
        except Exception:
            return {"title": "", "equip_list": [], "raw_dom": {}}


def format_equip_list(equip_data: dict[str, Any]) -> str:
    lines = ["===== 网易阴阳师藏宝阁装备数据 ====="]
    lines.append(f"状态: {'成功' if equip_data.get('success') else '失败'}")
    if equip_data.get("auth_state"):
        lines.append(f"认证状态: {equip_data['auth_state']}")
    if equip_data.get("scan_mode"):
        lines.append(
            "扫描模式: " + ("深度扫描" if equip_data["scan_mode"] == "full" else "增量扫描")
        )
    if not equip_data.get("success"):
        lines.append(f"错误信息: {equip_data.get('error', '无')}")
        if equip_data.get("captured_api_count") is not None:
            lines.append(f"捕获装备接口响应: {equip_data['captured_api_count']} 个")
        for diagnostic in equip_data.get("api_diagnostics", []):
            keys = ",".join(diagnostic.get("top_keys", [])) or "-"
            lists = ",".join(diagnostic.get("list_paths", [])) or "-"
            lines.append(
                f"接口结构: {diagnostic.get('endpoint')} | "
                f"status={diagnostic.get('status_code') or '-'} | "
                f"keys={keys} | lists={lists}"
            )
        if equip_data.get("needs_user_action"):
            lines.append("处理方式: 使用当前账号运行 python interactive_browser.py")
        return "\n".join(lines)

    lines.append(f"持久快照项目: {len(equip_data.get('equip_list', []))} 个")
    if equip_data.get("incremental_item_count") is not None:
        lines.append(f"本轮观察项目: {equip_data['incremental_item_count']} 个")
    if equip_data.get("changes_detected") is not None:
        lines.append(f"本轮新增/更新: {equip_data['changes_detected']} 个")
    stats = equip_data.get("storage_stats") or {}
    if stats:
        lines.append(
            f"数据库写入: 新增 {stats.get('inserted', 0)} / "
            f"更新 {stats.get('updated', 0)} / 未变化 {stats.get('unchanged', 0)}"
        )
    if equip_data.get("pages_scanned"):
        lines.append(
            f"滚动轮数: {equip_data['pages_scanned']} | "
            f"结束原因: {equip_data.get('termination_reason', '-')}"
        )
    if equip_data.get("snapshot_may_include_stale"):
        lines.append("快照提示: 按不删除策略保留历史项目，请结合 last_seen_at 判断新旧。")

    preview = equip_data.get("equip_list", [])[:20]
    for index, item in enumerate(preview, 1):
        lines.append(
            f"  [{index}] {item.get('name') or '未命名'} | "
            f"价格: {item.get('price') or '暂无'} | ID: {item.get('id') or '-'}"
        )
    remaining = len(equip_data.get("equip_list", [])) - len(preview)
    if remaining > 0:
        lines.append(f"  ……其余 {remaining} 项请查看 SQLite 预览页")
    return "\n".join(lines)
