import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cbg_fetcher import (
    LOGIN_AGREEMENT_CHECKBOX_SELECTOR,
    LOGIN_AGREEMENT_CONTROL_SELECTOR,
    LOGIN_AGREEMENT_ERROR_SELECTOR,
    CBGFetcher,
    _payload_indicates_end,
    _run_async,
    extract_structured_items,
    format_equip_list,
    get_business_error,
    get_business_status,
    is_business_success,
    is_equipment_api,
    parse_cbg_url,
    summarize_api_payloads,
)

TARGET_URL = "https://yys.cbg.163.com/cgi/mweb/pl?view_loc=equip_list&tfid=f_kingkong"


class FakeLocatorCollection:
    def __init__(self, items=()):
        self.items = list(items)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeAgreementElement:
    def __init__(
        self,
        *,
        visible=True,
        native=False,
        checked=False,
        class_name="",
        message="",
    ):
        self.visible = visible
        self.native = native
        self.checked = checked
        self.class_name = class_name
        self.message = message
        self.click_count = 0
        self.check_count = 0
        self.parent = None

    async def get_attribute(self, name):
        if name == "type":
            return "checkbox" if self.native else None
        if name == "class":
            return self.class_name
        if name == "aria-checked":
            return "true" if self.checked and not self.native else "false"
        return None

    async def is_checked(self, timeout=None):
        return self.checked

    async def check(self, force=False, timeout=None):
        self.check_count += 1
        self.checked = True

    async def is_visible(self, timeout=None):
        return self.visible

    async def click(self, force=False, timeout=None):
        self.click_count += 1
        self.checked = True
        self.class_name += " checked"

    async def inner_text(self, timeout=None):
        return self.message

    def locator(self, selector):
        if selector == "xpath=.." and self.parent is not None:
            return self.parent
        return FakeLocatorCollection()


class FakeVisualAgreementToggle(FakeAgreementElement):
    def __init__(self, checkbox):
        super().__init__(visible=True)
        self.checkbox = checkbox
        self.visual_checked = False
        checkbox.parent = self

    async def click(self, force=False, timeout=None):
        self.click_count += 1
        self.visual_checked = True
        self.checked = True
        self.checkbox.checked = True

    def locator(self, selector):
        if 'input[type="checkbox"]' in selector:
            return FakeLocatorCollection([self.checkbox])
        return FakeLocatorCollection()


class FakeAgreementRow:
    def __init__(self, checkbox):
        self.checkbox = checkbox

    async def count(self):
        return 1

    def locator(self, selector):
        if 'input[type="checkbox"]' in selector:
            return FakeLocatorCollection([self.checkbox])
        return FakeLocatorCollection()


class FakeAgreementLabel(FakeAgreementElement):
    def __init__(self, row, checkbox=None):
        super().__init__(visible=True, message="已阅读并同意《平台服务协议》和《隐私政策》")
        self.row = row
        self.checkbox = checkbox
        self.component_accepted = False

    async def click(self, force=False, timeout=None):
        self.click_count += 1
        self.component_accepted = True
        if self.checkbox is not None:
            self.checkbox.checked = True

    def locator(self, selector):
        if selector.startswith("xpath=ancestor-or-self::"):
            return self.row
        return FakeLocatorCollection()


class FakeAgreementFrame:
    def __init__(self, *, checkboxes=(), controls=(), errors=(), text=()):
        self.collections = {
            LOGIN_AGREEMENT_CHECKBOX_SELECTOR: FakeLocatorCollection(checkboxes),
            LOGIN_AGREEMENT_CONTROL_SELECTOR: FakeLocatorCollection(controls),
            LOGIN_AGREEMENT_ERROR_SELECTOR: FakeLocatorCollection(errors),
        }
        self.text = list(text)

    def locator(self, selector):
        return self.collections.get(selector, FakeLocatorCollection())

    def get_by_text(self, marker, exact=False):
        matches = [element for element in self.text if marker in element.message]
        return FakeLocatorCollection(matches)


class CurrentNetEaseAgreementFrame:
    """Minimal model of the public 163 login DOM observed on 2026-08-12."""

    def __init__(self, checkbox):
        self.checkbox = checkbox

    def locator(self, selector):
        if '.fur-agree-4 input[type="checkbox"]' in selector:
            return FakeLocatorCollection([self.checkbox])
        return FakeLocatorCollection()


def test_relative_profile_path_is_not_based_on_process_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fetcher = CBGFetcher(user_data_dir="browser_profiles/one")
    assert Path(fetcher.user_data_dir) == (
        Path(__file__).resolve().parents[1] / "browser_profiles" / "one"
    )


def test_url_parser_does_not_conflate_tracking_and_order_id():
    params = parse_cbg_url(TARGET_URL + "&refer_sn=tracking")
    assert params["refer_sn"] == "tracking"
    assert "ordersn" not in params


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/cgi-bin/query.py",
        "http://yys.cbg.163.com/cgi-bin/query.py",
        "https://yys.cbg.163.com/cgi-bin/query.py.evil",
        "https://yys.cbg.163.com/cgi/api/get_user_data",
    ],
)
def test_api_allowlist_rejects_foreign_or_similar_urls(url):
    assert is_equipment_api(url) is False


def test_api_allowlist_accepts_expected_equipment_paths():
    assert is_equipment_api("https://yys.cbg.163.com/cgi-bin/query.py?page=2")
    assert is_equipment_api("https://yys.cbg.163.com/cgi/api/equip/list?page=2")


def test_generic_id_only_metadata_is_not_treated_as_listing():
    items = extract_structured_items(
        [
            {
                "sequence": 1,
                "url": "/cgi-bin/query.py",
                "json": {"status": 1, "data": [{"id": "category-1"}]},
            }
        ],
        [],
    )
    assert items == []


def test_business_status_handles_explicit_failure_without_status_code():
    assert get_business_status({"status_code": "mobile_auth"}) == "MOBILE_AUTH"
    assert is_business_success({"status": 1})
    assert get_business_error({"status": 6}) == "STATUS_6"
    assert get_business_error({"status": 1}) == ""


def test_explicit_pagination_end_requires_known_fields():
    assert _payload_indicates_end({"result": {"has_more": False}})
    assert _payload_indicates_end({"data": {"is_end": True}})
    assert not _payload_indicates_end({"has_more": True})
    assert not _payload_indicates_end({"complete": True})


def test_schema_diagnostics_never_include_field_values():
    captured = [
        {
            "sequence": 1,
            "url": "/cgi-bin/query.py",
            "json": {
                "status": 1,
                "access_token": "must-not-appear",
                "result": {"rows": [{"unknown": "secret-value"}]},
            },
        }
    ]
    serialized = json.dumps(summarize_api_payloads(captured), ensure_ascii=False)
    assert "$.result.rows[1]" in serialized
    assert "must-not-appear" not in serialized
    assert "secret-value" not in serialized


def test_dom_fallback_does_not_duplicate_matching_api_display():
    captured = [
        {
            "sequence": 1,
            "url": "/cgi-bin/query.py",
            "json": {"equip_list": [{"equip_id": "A", "equip_name": "项目", "price": "100"}]},
        }
    ]
    items = extract_structured_items(
        captured,
        [
            {"name": "项目", "price": "100"},
            {"name": "DOM 独有", "price": "200"},
        ],
    )
    assert len(items) == 2
    assert sum(not item["identity_stable"] for item in items) == 1


def test_common_data_list_wrapper_is_parsed():
    items = extract_structured_items(
        [
            {
                "sequence": 1,
                "url": "/cgi-bin/query.py",
                "json": {
                    "status": 1,
                    "data": [{"equip_id": "A", "equip_name": "项目", "price": 1}],
                },
            }
        ],
        [],
    )
    assert [item["id"] for item in items] == ["A"]


def test_sync_wrapper_rejects_nested_event_loop_cleanly():
    async def sample():
        return 1

    async def run():
        with pytest.raises(RuntimeError, match="async_"):
            _run_async(sample())

    asyncio.run(run())


def test_login_agreement_checks_all_matching_native_inputs():
    first = FakeAgreementElement(native=True)
    second = FakeAgreementElement(native=True)
    frame = FakeAgreementFrame(checkboxes=[first, second])

    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame)) is True
    assert first.checked is True
    assert second.checked is True
    assert first.check_count == second.check_count == 1


def test_login_agreement_covers_current_netease_sibling_checkbox_dom():
    checkbox = FakeAgreementElement(native=True)
    frame = CurrentNetEaseAgreementFrame(checkbox)
    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame)) is True
    assert checkbox.checked is True


def test_login_agreement_triggers_email_form_component_handler():
    checkbox = FakeAgreementElement(native=True)
    visual_toggle = FakeVisualAgreementToggle(checkbox)
    label = FakeAgreementLabel(FakeAgreementRow(checkbox), checkbox)
    frame = FakeAgreementFrame(
        checkboxes=[checkbox],
        controls=[visual_toggle],
        text=[label],
    )

    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame)) is True
    assert label.click_count == 1
    assert label.component_accepted is True
    assert visual_toggle.click_count == 0
    assert checkbox.checked is True
    assert checkbox.check_count == 0


def test_forced_agreement_retry_triggers_handler_even_if_dom_is_checked():
    checkbox = FakeAgreementElement(native=True, checked=True)
    label = FakeAgreementLabel(FakeAgreementRow(checkbox), checkbox)
    frame = FakeAgreementFrame(checkboxes=[checkbox], text=[label])

    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame, force=True)) is True
    assert label.click_count == 1
    assert label.component_accepted is True


def test_login_agreement_skips_hidden_first_control_and_clicks_visible_one():
    hidden = FakeAgreementElement(visible=False)
    visible = FakeAgreementElement(visible=True)
    frame = FakeAgreementFrame(controls=[hidden, visible])

    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame)) is True
    assert hidden.click_count == 0
    assert visible.click_count == 1


def test_login_agreement_error_only_uses_visible_error_message():
    hidden = FakeAgreementElement(visible=False, message="请先勾选登录协议")
    visible = FakeAgreementElement(visible=True, message="请先阅读并同意服务条款")
    frame = FakeAgreementFrame(errors=[hidden, visible])
    assert asyncio.run(CBGFetcher._login_agreement_error_visible(frame)) is True


def test_login_agreement_detects_current_plain_text_warning():
    warning = FakeAgreementElement(
        visible=True,
        message="您需要同意相关条款才能登录",
    )
    frame = FakeAgreementFrame(text=[warning])
    assert asyncio.run(CBGFetcher._login_agreement_error_visible(frame)) is True


def _mock_first_locator(*, visible=True):
    locator = MagicMock()
    locator.first = locator
    locator.is_visible = AsyncMock(return_value=visible)
    locator.click = AsyncMock()
    locator.fill = AsyncMock()
    return locator


def test_login_rechecks_agreement_prompt_and_submits_again():
    account_tab = _mock_first_locator(visible=False)
    username_input = _mock_first_locator()
    password_input = _mock_first_locator()
    login_button = _mock_first_locator()
    login_frame = MagicMock()
    login_frame.url = "https://reg.163.com/login"

    def locate(selector):
        if selector == "div.u-head1":
            return account_tab
        if selector.startswith('input[name="email"]'):
            return username_input
        if selector.startswith('input[name="password"]'):
            return password_input
        if selector.startswith("a.u-loginbtn"):
            return login_button
        raise AssertionError(f"unexpected selector: {selector}")

    login_frame.locator.side_effect = locate
    page = MagicMock()
    page.frames = [login_frame]
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.wait_for_url = AsyncMock()
    context = MagicMock()
    context.cookies = AsyncMock(return_value=[])
    fetcher = CBGFetcher()
    fetcher._ensure_login_agreement = AsyncMock(return_value=True)
    fetcher._login_agreement_error_visible = AsyncMock(side_effect=[True, False])

    result = asyncio.run(
        fetcher._do_login_in_page(page, context, "temporary-user", "temporary-password")
    )

    assert result is True
    assert login_button.click.await_count == 2
    assert fetcher._ensure_login_agreement.await_args_list[1].kwargs == {"force": True}


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.url = "https://yys.cbg.163.com/cgi-bin/query.py?page=1"
        self._payload = payload

    async def json(self):
        return self._payload


class FakeLocator:
    async def inner_text(self, timeout=None):
        return "装备列表"


class FakePage:
    def __init__(self, payload):
        self.url = TARGET_URL
        self.payload = payload
        self.listener = None
        self.closed = False

    def on(self, event, callback):
        assert event == "response"
        self.listener = callback

    def remove_listener(self, event, callback):
        assert event == "response"
        assert callback is self.listener

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self.listener(FakeResponse(self.payload))

    def locator(self, selector):
        return FakeLocator()

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def test_fetch_context_waits_for_response_and_accepts_explicit_api_end():
    payload = {
        "status": 1,
        "has_more": False,
        "equip_list": [{"equip_id": "A", "equip_name": "项目", "price": 100}],
    }
    page = FakePage(payload)
    fetcher = CBGFetcher()
    fetcher._async_extract_dom_equip_info = AsyncMock(
        return_value={
            "title": "测试",
            "equip_list": [],
            "raw_dom": {},
        }
    )
    fetcher._page_requires_mobile_auth = AsyncMock(return_value=False)

    result = asyncio.run(
        fetcher._async_fetch_in_context(
            FakeContext(page), TARGET_URL, "user", "password", 20, 1000, 3, []
        )
    )

    assert result["success"] is True
    assert result["equip_list"][0]["id"] == "A"
    assert result["scan_complete"] is True
    assert result["termination_reason"] == "api_end"
    assert page.closed is True


def test_fetch_context_treats_confirmed_empty_list_as_success():
    page = FakePage({"status": 1, "has_more": False, "equip_list": []})
    fetcher = CBGFetcher()
    fetcher._async_extract_dom_equip_info = AsyncMock(
        return_value={
            "title": "测试",
            "equip_list": [],
            "raw_dom": {},
        }
    )
    fetcher._page_requires_mobile_auth = AsyncMock(return_value=False)
    result = asyncio.run(
        fetcher._async_fetch_in_context(FakeContext(page), TARGET_URL, None, None, 5, 1000, 2, [])
    )
    assert result["success"] is True
    assert result["equip_list"] == []


def test_browser_launch_retries_transient_profile_failure(tmp_path):
    fake_context = object()
    fake_chromium = type("FakeChromium", (), {})()
    fake_chromium.launch_persistent_context = AsyncMock(
        side_effect=[RuntimeError("profile locked"), fake_context]
    )
    fake_playwright = type("FakePlaywright", (), {"chromium": fake_chromium})()
    fetcher = CBGFetcher(
        user_data_dir=str(tmp_path),
        browser_start_attempts=3,
        browser_retry_delay_seconds=2,
    )

    async def run():
        with patch("cbg_fetcher.asyncio.sleep", new=AsyncMock()) as sleep:
            context = await fetcher._launch_persistent_context(fake_playwright, False)
            sleep.assert_awaited_once_with(2)
            return context

    assert asyncio.run(run()) is fake_context
    assert fake_chromium.launch_persistent_context.await_count == 2


def test_polling_waits_restart_delay_before_launching_browser(tmp_path):
    events = []
    fake_context = type("FakeContext", (), {"close": AsyncMock()})()
    fake_chromium = type("FakeChromium", (), {})()

    async def launch(**_options):
        events.append("launch")
        return fake_context

    fake_chromium.launch_persistent_context = AsyncMock(side_effect=launch)
    fake_playwright = type("FakePlaywright", (), {"chromium": fake_chromium})()

    class Manager:
        async def __aenter__(self):
            return fake_playwright

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    fetcher = CBGFetcher(user_data_dir=str(tmp_path))
    fetcher._async_fetch_in_context = AsyncMock(
        return_value={"success": True, "auth_state": "authenticated", "equip_list": []}
    )

    async def sleep(delay):
        events.append(("sleep", delay))

    async def run():
        with (
            patch("cbg_fetcher.async_playwright", return_value=Manager()),
            patch("cbg_fetcher.asyncio.sleep", side_effect=sleep),
        ):
            return await fetcher.async_poll_equip_data(
                url=TARGET_URL,
                username="user",
                password="password",
                result_handler=lambda _result, _cycle: None,
                max_cycles=1,
                initial_delay_seconds=45,
            )

    assert asyncio.run(run())["success"] is True
    assert events[:2] == [("sleep", 45.0), "launch"]


def test_polling_reuses_context_and_restores_cycle_checkpoint(tmp_path):
    fake_context = type("FakeContext", (), {"close": AsyncMock()})()
    fake_chromium = type("FakeChromium", (), {})()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_context)
    fake_playwright = type("FakePlaywright", (), {"chromium": fake_chromium})()

    class Manager:
        async def __aenter__(self):
            return fake_playwright

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    fetcher = CBGFetcher(user_data_dir=str(tmp_path), browser_channel="chrome")
    fetcher._async_fetch_in_context = AsyncMock(
        side_effect=[
            {"success": True, "auth_state": "authenticated", "equip_list": []},
            {"success": True, "auth_state": "authenticated", "equip_list": []},
        ]
    )
    handled = []
    started = []

    async def cycle_started(cycle, mode, started_at):
        started.append((cycle, mode, bool(started_at)))
        return cycle * 10

    async def run():
        with (
            patch("cbg_fetcher.async_playwright", return_value=Manager()),
            patch("cbg_fetcher.asyncio.sleep", new=AsyncMock()),
        ):
            return await fetcher.async_poll_equip_data(
                url=TARGET_URL,
                username="user",
                password="password",
                result_handler=lambda result, cycle: handled.append(
                    (cycle, result["scan_mode"], result["_run_id"])
                ),
                interval_seconds=60,
                full_refresh_interval_seconds=3600,
                max_cycles=2,
                checkpoint={
                    "last_cycle": 7,
                    "last_full_scan_at": "2999-01-01T00:00:00+00:00",
                    "last_full_scan_complete": 1,
                },
                cycle_started_handler=cycle_started,
            )

    result = asyncio.run(run())
    assert result["success"] is True
    assert handled == [(8, "incremental", 80), (9, "incremental", 90)]
    assert started == [(8, "incremental", True), (9, "incremental", True)]
    assert fake_chromium.launch_persistent_context.await_count == 1
    assert fetcher._async_fetch_in_context.await_count == 2
    fake_context.close.assert_awaited_once()


def test_formatting_describes_non_deleting_snapshot():
    formatted = format_equip_list(
        {
            "success": True,
            "auth_state": "authenticated",
            "equip_list": [{"id": "A", "name": "项目", "price": "100"}],
            "snapshot_may_include_stale": True,
        }
    )
    assert "项目" in formatted
    assert "不删除策略" in formatted
