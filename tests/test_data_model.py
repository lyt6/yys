import json

from cbg_fetcher import extract_structured_items
from data_model import (
    count_equipment_changes,
    deduplicate_items,
    equipment_fingerprint,
    merge_equipment_snapshots,
    normalize_equipment_item,
    sanitize_sensitive_data,
)


def captured(sequence, *items):
    return {
        "url": "/cgi-bin/query.py",
        "sequence": sequence,
        "json": {"status": 1, "equip_list": list(items)},
    }


def test_later_response_for_same_id_wins():
    result = extract_structured_items(
        [
            captured(1, {"equip_id": "A", "equip_name": "项目", "price": 100}),
            captured(2, {"equip_id": "A", "equip_name": "项目", "price": 200}),
        ],
        [],
    )

    assert len(result) == 1
    assert result[0]["price"] == "200"
    assert result[0]["response_sequence"] == 2


def test_distinct_ids_are_not_collapsed_by_same_name_and_price():
    result = extract_structured_items(
        [
            captured(
                1,
                {"equip_id": "A", "equip_name": "同名", "price": 100},
                {"equip_id": "B", "equip_name": "同名", "price": 100},
            )
        ],
        [],
    )

    assert {item["id"] for item in result} == {"A", "B"}


def test_duplicate_fresh_items_are_deduplicated_before_merge_and_count():
    previous = [
        normalize_equipment_item(
            {"equip_id": "A", "equip_name": "项目", "price": 100}, source="api"
        )
    ]
    fresh = [
        normalize_equipment_item(
            {"equip_id": "A", "equip_name": "项目", "price": 150}, source="api"
        ),
        normalize_equipment_item(
            {"equip_id": "A", "equip_name": "项目", "price": 200}, source="api"
        ),
    ]

    merged = merge_equipment_snapshots(previous, fresh)
    assert len(merged) == 1
    assert merged[0]["price"] == "200"
    assert count_equipment_changes(previous, fresh) == 1


def test_upsert_never_deletes_unobserved_items():
    previous = [
        normalize_equipment_item({"equip_id": "A", "name": "A", "price": 1}, source="api"),
        normalize_equipment_item({"equip_id": "B", "name": "B", "price": 2}, source="api"),
    ]
    fresh = [
        normalize_equipment_item({"equip_id": "A", "name": "A2", "price": 3}, source="api"),
        normalize_equipment_item({"equip_id": "C", "name": "C", "price": 4}, source="api"),
    ]

    merged = merge_equipment_snapshots(previous, fresh)
    assert [item["id"] for item in merged] == ["A", "C", "B"]


def test_volatile_fields_do_not_trigger_change_but_price_does():
    first = normalize_equipment_item(
        {
            "equip_id": "A",
            "name": "项目",
            "price": 100,
            "server_time": 1,
            "trace_id": "one",
            "rank": 5,
        },
        source="api",
    )
    volatile_only = normalize_equipment_item(
        {
            "equip_id": "A",
            "name": "项目",
            "price": 100,
            "server_time": 2,
            "trace_id": "two",
            "rank": 8,
        },
        source="api",
    )
    changed = normalize_equipment_item(
        {
            "equip_id": "A",
            "name": "项目",
            "price": 101,
            "server_time": 3,
        },
        source="api",
    )

    assert equipment_fingerprint(first) == equipment_fingerprint(volatile_only)
    assert count_equipment_changes([first], [volatile_only]) == 0
    assert count_equipment_changes([first], [changed]) == 1


def test_fallback_identity_is_explicitly_marked_unstable():
    item = normalize_equipment_item({"name": "没有业务 ID", "level": 15, "price": 10}, source="dom")
    assert item["identity"].startswith("display:")
    assert item["identity_stable"] is False
    assert item["id"] == ""


def test_sensitive_values_are_removed_recursively():
    value = {
        "access_token": "secret",
        "wallet_data": {"balance": 1},
        "items": [{"name": "安全字段", "mobile": "secret", "level": 15}],
    }
    clean = sanitize_sensitive_data(value)
    serialized = json.dumps(clean, ensure_ascii=False)
    assert "secret" not in serialized
    assert clean["items"][0]["level"] == 15


def test_deduplication_is_id_based_not_display_based():
    items = [
        normalize_equipment_item({"equip_id": "A", "name": "同名", "price": 1}, source="api"),
        normalize_equipment_item({"equip_id": "B", "name": "同名", "price": 1}, source="api"),
    ]
    assert len(deduplicate_items(items)) == 2
