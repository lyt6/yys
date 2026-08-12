import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cbg_fetcher import (
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
