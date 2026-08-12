import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cbg_fetcher import (
    EMAIL_AGREEMENT_CONTROL_SELECTOR,
    EMAIL_AGREEMENT_SELECTED_CLASS,
    LOGIN_AGREEMENT_CHECKBOX_SELECTOR,
    LOGIN_AGREEMENT_CONTROL_SELECTOR,
    LOGIN_AGREEMENT_ERROR_SELECTOR,
    CBGFetcher,
    _payload_indicates_end,
    _payload_reported_total,
    _run_async,
    extract_structured_items,
    format_equip_list,
    get_business_error,
    get_business_status,
    is_business_success,
    is_equipment_api,
    is_full_query_api,
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
    def __init__(
        self,
        *,
        checkboxes=(),
        controls=(),
        email_controls=(),
        errors=(),
        text=(),
    ):
        self.collections = {
            LOGIN_AGREEMENT_CHECKBOX_SELECTOR: FakeLocatorCollection(checkboxes),
            LOGIN_AGREEMENT_CONTROL_SELECTOR: FakeLocatorCollection(controls),
            EMAIL_AGREEMENT_CONTROL_SELECTOR: FakeLocatorCollection(email_controls),
            LOGIN_AGREEMENT_ERROR_SELECTOR: FakeLocatorCollection(errors),
        }
        self.text = list(text)

    def locator(self, selector):
        return self.collections.get(selector, FakeLocatorCollection())

    def get_by_text(self, marker, exact=False):
        matches = [element for element in self.text if marker in element.message]
        return FakeLocatorCollection(matches)


class FakeEmailAgreementControl:
    """Model the current email skin's inverse hidden-input semantics."""

    def __init__(self, *, selected=False, visible=True):
        self.selected = selected
        self.visible = visible
        self.hidden_input_checked = not selected
        self.click_count = 0

    async def is_visible(self, timeout=None):
        return self.visible

    async def get_attribute(self, name):
        if name == "class":
            classes = "u-dl-agree j-mail-clause-span"
            if self.selected:
                classes += f" {EMAIL_AGREEMENT_SELECTED_CLASS}"
            return classes
        return None

    async def click(self, force=False, timeout=None):
        self.click_count += 1
        self.selected = not self.selected
        self.hidden_input_checked = not self.selected


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
    assert is_equipment_api("https://yys.cbg.163.com/cgi/api/query?page=2")
    assert is_equipment_api("https://yys.cbg.163.com/cgi/api/equip/list?page=2")


def test_full_query_classifier_excludes_recommendation_feed():
    assert is_full_query_api("https://yys.cbg.163.com/cgi/api/query?page=2")
    assert is_full_query_api("/cgi-bin/query.py")
    assert not is_full_query_api("https://yys.cbg.163.com/cgi-bin/recommend.py")


def test_reported_total_uses_only_explicit_total_fields():
    assert _payload_reported_total({"total_num": 10000, "result": [1, 2]}) == 10000
    assert _payload_reported_total({"paging": {"total_count": "9,876"}}) == 9876
    assert _payload_reported_total({"result": [1, 2]}) is None


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
    assert _payload_indicates_end({"paging": {"is_last_page": True}})
    assert _payload_indicates_end({"pager": {"has_next_page": False}})
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
            "url": "/cgi/api/equip/list",
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


def test_current_recommend_result_items_are_parsed():
    items = extract_structured_items(
        [
            {
                "sequence": 1,
                "url": "/cgi-bin/recommend.py",
                "json": {
                    "status": "OK",
                    "status_code": "OK",
                    "paging": {"is_last_page": True},
                    "result": [
                        {
                            "serverid": "server-a",
                            "game_ordersn": "order-a",
                            "equipid": "equip-a",
                            "desc_sumup_short": "账号摘要",
                            "format_equip_name": "角色名",
                            "price": 18800,
                            "other_info": {
                                "level_desc": "60级",
                                "basic_attrs": [],
                                "highlights": [],
                            },
                        }
                    ],
                },
            }
        ],
        [],
    )

    assert len(items) == 1
    assert items[0]["identity"] == "server_serial:server-a:order-a"
    assert items[0]["id"] == "server-a:order-a"
    assert items[0]["id_kind"] == "server_serial"
    assert items[0]["name"] == "账号摘要"
    assert items[0]["price"] == "188.00"
    assert items[0]["detail"]["price"] == 18800
    assert items[0]["level"] == "60级"


def test_recommend_items_with_same_order_on_different_servers_remain_distinct():
    payload = {
        "status_code": "OK",
        "result": [
            {
                "serverid": server,
                "game_ordersn": "same-order",
                "price": 100,
                "other_info": {"format_equip_name": f"角色-{server}"},
            }
            for server in ("server-a", "server-b")
        ],
    }
    items = extract_structured_items(
        [{"sequence": 1, "url": "/cgi-bin/recommend.py", "json": payload}],
        [],
    )

    assert {item["identity"] for item in items} == {
        "server_serial:server-a:same-order",
        "server_serial:server-b:same-order",
    }


def test_recommend_eid_is_preferred_and_nested_name_is_supported():
    items = extract_structured_items(
        [
            {
                "sequence": 1,
                "url": "/cgi-bin/recommend.py",
                "json": {
                    "status_code": "OK",
                    "result": [
                        {
                            "eid": "stable-eid",
                            "serverid": "server-a",
                            "equip_sn": "equip-serial",
                            "price_total": 9900,
                            "other_info": {
                                "format_equip_name": "嵌套角色名",
                                "level_desc": "55级",
                            },
                        }
                    ],
                },
            }
        ],
        [],
    )

    assert items[0]["identity"] == "eid:stable-eid"
    assert items[0]["name"] == "嵌套角色名"
    assert items[0]["price"] == "99.00"


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


def test_email_agreement_uses_selected_class_not_inverse_hidden_input():
    control = FakeEmailAgreementControl(selected=False)
    assert control.hidden_input_checked is True
    frame = FakeAgreementFrame(email_controls=[control])

    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame)) is True
    assert control.selected is True
    assert control.hidden_input_checked is False
    assert control.click_count == 1


def test_email_agreement_does_not_reclick_already_selected_control():
    control = FakeEmailAgreementControl(selected=True)
    frame = FakeAgreementFrame(email_controls=[control])

    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame)) is True
    assert control.selected is True
    assert control.click_count == 0


def test_forced_email_agreement_retry_finishes_selected():
    control = FakeEmailAgreementControl(selected=True)
    frame = FakeAgreementFrame(email_controls=[control])

    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame, force=True)) is True
    assert control.selected is True
    assert control.hidden_input_checked is False
    assert control.click_count == 2


def test_forced_email_agreement_retry_clicks_once_when_unselected():
    control = FakeEmailAgreementControl(selected=False)
    frame = FakeAgreementFrame(email_controls=[control])

    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame, force=True)) is True
    assert control.selected is True
    assert control.click_count == 1


def test_login_agreement_covers_current_netease_sibling_checkbox_dom():
    checkbox = FakeAgreementElement(native=True)
    frame = CurrentNetEaseAgreementFrame(checkbox)
    assert asyncio.run(CBGFetcher()._ensure_login_agreement(frame)) is True
    assert checkbox.checked is True


def test_login_agreement_triggers_generic_component_handler():
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


def test_forced_generic_agreement_retry_triggers_handler():
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

    def __init__(self, payload, url="https://yys.cbg.163.com/cgi-bin/query.py?page=1"):
        self.url = url
        self._payload = payload

    async def json(self):
        return self._payload


class FakeLocator:
    async def inner_text(self, timeout=None):
        return "装备列表"


class FakePage:
    def __init__(self, payload, response_url="https://yys.cbg.163.com/cgi-bin/query.py?page=1"):
        self.url = TARGET_URL
        self.payload = payload
        self.response_url = response_url
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
        self.listener(FakeResponse(self.payload, self.response_url))

    def locator(self, selector):
        return FakeLocator()

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def test_full_query_mode_is_installed_before_navigation():
    context = MagicMock()
    context.add_init_script = AsyncMock()
    fetcher = CBGFetcher(force_full_query=True)

    assert asyncio.run(fetcher._install_full_query_mode(context)) is True
    script = context.add_init_script.await_args.kwargs["script"]
    assert "open_recommd" in script
    assert "value: false" in script


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


def test_full_query_reported_total_marks_complete_without_recommendation_semantics():
    page = FakePage(
        {
            "status": 1,
            "total_num": 1,
            "paging": {"is_last_page": False},
            "result": [{"eid": "A", "format_equip_name": "项目", "price": 100}],
        },
        response_url="https://yys.cbg.163.com/cgi/api/query?page=1",
    )
    fetcher = CBGFetcher()
    fetcher._async_extract_dom_equip_info = AsyncMock(
        return_value={"title": "测试", "equip_list": [], "raw_dom": {}}
    )
    fetcher._page_requires_mobile_auth = AsyncMock(return_value=False)

    result = asyncio.run(
        fetcher._async_fetch_in_context(FakeContext(page), TARGET_URL, None, None, 5, 1000, 2, [])
    )

    assert result["success"] is True
    assert result["collection_mode"] == "full_query"
    assert result["reported_total"] == 1
    assert result["scan_complete"] is True
    assert result["termination_reason"] == "reported_total"


def test_recommendation_end_is_not_reported_as_full_coverage():
    page = FakePage(
        {
            "status": "OK",
            "status_code": "OK",
            "paging": {"is_last_page": True},
            "result": [
                {
                    "serverid": "server-a",
                    "game_ordersn": "order-a",
                    "format_equip_name": "推荐项目",
                    "price": 100,
                }
            ],
        },
        response_url="https://yys.cbg.163.com/cgi-bin/recommend.py?page=100",
    )
    fetcher = CBGFetcher()
    fetcher._async_extract_dom_equip_info = AsyncMock(
        return_value={"title": "测试", "equip_list": [], "raw_dom": {}}
    )
    fetcher._page_requires_mobile_auth = AsyncMock(return_value=False)

    result = asyncio.run(
        fetcher._async_fetch_in_context(FakeContext(page), TARGET_URL, None, None, 1, 1000, 2, [])
    )

    assert result["success"] is True
    assert result["collection_mode"] == "recommendation_fallback"
    assert result["scan_complete"] is False
    assert result["termination_reason"] == "recommendation_end"


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
