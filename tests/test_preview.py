import json
import threading
from urllib.parse import quote
from urllib.request import urlopen

import pytest

from data_model import normalize_equipment_item
from preview_server import build_server
from storage import SQLiteStore, target_key_for_url, utc_now

URL = "https://yys.cbg.163.com/cgi/mweb/pl?view_loc=equip_list"


def seed(path):
    store = SQLiteStore(str(path))
    target = target_key_for_url(URL)
    item = normalize_equipment_item(
        {"equip_id": "A", "equip_name": "测试项目", "price": 188}, source="api"
    )
    now = utc_now()
    store.record_result(
        "demo",
        target,
        URL,
        {
            "success": True,
            "auth_state": "authenticated",
            "scan_mode": "full",
            "cycle": 1,
            "started_at": now,
            "fetched_at": now,
            "scan_complete": True,
            "termination_reason": "api_end",
            "pages_scanned": 1,
            "observed_equip_list": [item],
        },
    )


def test_preview_server_serves_ui_and_read_only_json_api(tmp_path):
    database = tmp_path / "preview.sqlite3"
    seed(database)
    server = build_server(str(database), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(base + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
            assert response.headers["Content-Security-Policy"]
            assert "藏宝阁数据预览" in html
        with urlopen(base + "/api/items?q=" + quote("测试"), timeout=5) as response:
            payload = json.load(response)
            assert payload["total"] == 1
            assert payload["items"][0]["id"] == "A"
        with urlopen(base + "/api/summary", timeout=5) as response:
            assert json.load(response)["total"] == 1
        with urlopen(base + "/api/runs", timeout=5) as response:
            assert len(json.load(response)["runs"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_preview_server_rejects_remote_bind_without_explicit_opt_in(tmp_path):
    with pytest.raises(ValueError, match="没有身份认证"):
        build_server(str(tmp_path / "preview.sqlite3"), "0.0.0.0", 0)
