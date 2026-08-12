"""Shared listing identity, sanitisation and snapshot helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

ID_FIELDS = (
    "eid",
    "game_ordersn",
    "equip_sn",
    "item_sn",
    "equipid",
    "equip_id",
    "listing_id",
    "ordersn",
    "order_sn",
    "id",
    "sn",
    "role_id",
    "roleid",
)

SENSITIVE_KEYS = {
    "account",
    "cookie",
    "cookies",
    "email",
    "login_info",
    "mobile",
    "password",
    "phone",
    "safe_code",
    "session",
    "sid",
    "token",
    "urs_mobile",
    "wallet_data",
}

SENSITIVE_KEY_FRAGMENTS = (
    "account",
    "cookie",
    "email",
    "login_info",
    "mobile",
    "password",
    "phone",
    "safe_code",
    "session",
    "token",
    "wallet",
)

VOLATILE_KEYS = {
    "_request_id",
    "page_session_id",
    "server_time",
    "trace_id",
    "request_id",
    "request_time",
    "recommend_score",
    "reco_request_id",
    "rank",
    "position",
    "page_index",
    "expire_remain_seconds",
    "view_count",
    "click_count",
    "collect_count",
    "online_num",
}

INTERNAL_ITEM_KEYS = {
    "content_hash",
    "identity",
    "identity_stable",
    "id_kind",
    "response_sequence",
    "source",
}


def is_sensitive_key(key: str) -> bool:
    normalized = str(key).lower()
    return normalized in SENSITIVE_KEYS or any(
        fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS
    )


def sanitize_sensitive_data(value: Any) -> Any:
    """Recursively remove common authentication and account fields."""
    if isinstance(value, dict):
        return {
            str(key): sanitize_sensitive_data(item)
            for key, item in value.items()
            if not is_sensitive_key(str(key))
        }
    if isinstance(value, list):
        return [sanitize_sensitive_data(item) for item in value]
    return value


def _scalar_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _display_price(value: Any, source: str) -> str:
    """Convert the known recommend endpoint's cent price while retaining raw detail."""
    text = _scalar_text(value)
    if not text or "/cgi-bin/recommend.py" not in str(source).lower():
        return text
    try:
        return f"{Decimal(text) / Decimal(100):.2f}"
    except InvalidOperation:
        return text


def extract_raw_identity(raw: dict[str, Any]) -> tuple[str, str, str, bool]:
    """Return canonical identity, display ID, ID kind and stability."""
    eid = _scalar_text(raw.get("eid"))
    if eid:
        return f"eid:{eid}", eid, "eid", True

    server_id = _scalar_text(raw.get("serverid") or raw.get("server_id"))
    for key in ("game_ordersn", "equip_sn", "item_sn"):
        serial = _scalar_text(raw.get(key))
        if server_id and serial:
            value = f"{server_id}:{serial}"
            return f"server_serial:{value}", value, "server_serial", True

    for key in ID_FIELDS:
        if key == "eid":
            continue
        value = _scalar_text(raw.get(key))
        if value:
            normalized_key = "ordersn" if key == "order_sn" else key
            return f"{normalized_key}:{value}", value, normalized_key, True

    other_info = raw.get("other_info")
    nested = other_info if isinstance(other_info, dict) else {}
    name = _scalar_text(
        raw.get("desc_sumup_short")
        or nested.get("format_equip_name")
        or raw.get("format_equip_name")
        or raw.get("equip_name")
        or raw.get("name")
        or raw.get("title")
    )
    level = _scalar_text(raw.get("level") or raw.get("equip_level"))
    basis = f"{name}\x1f{level}"
    if not name:
        basis = json.dumps(
            sanitize_sensitive_data(raw),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return f"display:{digest}", "", "display", False


def normalize_equipment_item(
    raw: dict[str, Any],
    *,
    source: str,
    response_sequence: int = 0,
) -> dict[str, Any]:
    clean_raw = sanitize_sensitive_data(raw)
    identity, display_id, id_kind, stable = extract_raw_identity(clean_raw)
    other_info = clean_raw.get("other_info")
    nested = other_info if isinstance(other_info, dict) else {}
    name = _scalar_text(
        clean_raw.get("desc_sumup_short")
        or nested.get("format_equip_name")
        or clean_raw.get("format_equip_name")
        or clean_raw.get("equip_name")
        or clean_raw.get("name")
        or clean_raw.get("title")
    )
    raw_price = (
        clean_raw.get("price_total")
        if clean_raw.get("price_total") not in (None, "")
        else clean_raw.get("price") or clean_raw.get("price_desc")
    )
    price = _display_price(raw_price, source)
    level = _scalar_text(
        clean_raw.get("level")
        or clean_raw.get("equip_level")
        or nested.get("level_desc")
    )
    item = {
        "identity": identity,
        "identity_stable": stable,
        "id": display_id,
        "id_kind": id_kind,
        "name": name,
        "price": price,
        "level": level,
        "detail": clean_raw,
        "source": source,
        "response_sequence": int(response_sequence or 0),
    }
    item["content_hash"] = equipment_fingerprint(item)
    return item


def equipment_identity_key(item: dict[str, Any]) -> str:
    identity = _scalar_text(item.get("identity"))
    if identity:
        return identity

    detail = item.get("detail")
    raw: dict[str, Any] = dict(detail) if isinstance(detail, dict) else {}
    item_id = _scalar_text(item.get("id"))
    if item_id:
        id_kind = _scalar_text(item.get("id_kind")) or "id"
        raw[id_kind] = item_id
    raw.setdefault("name", item.get("name"))
    raw.setdefault("level", item.get("level"))
    return extract_raw_identity(raw)[0]


def _clean_for_fingerprint(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _clean_for_fingerprint(child)
            for key, child in value.items()
            if str(key).lower() not in VOLATILE_KEYS
            and str(key) not in INTERNAL_ITEM_KEYS
            and not is_sensitive_key(str(key))
        }
    if isinstance(value, list):
        return [_clean_for_fingerprint(child) for child in value]
    return value


def equipment_fingerprint(item: dict[str, Any]) -> str:
    semantic = {
        "name": item.get("name", ""),
        "price": item.get("price", ""),
        "level": item.get("level", ""),
        "detail": item.get("detail", {}),
    }
    payload = json.dumps(
        _clean_for_fingerprint(semantic),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deduplicate_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by canonical identity; the last observation wins."""
    latest: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = equipment_identity_key(item)
        normalized = dict(item)
        normalized["identity"] = identity
        normalized.setdefault("identity_stable", not identity.startswith("display:"))
        normalized["content_hash"] = equipment_fingerprint(normalized)
        if identity in latest:
            latest.pop(identity)
        latest[identity] = normalized
    return list(latest.values())


def count_equipment_changes(
    known_items: Iterable[dict[str, Any]],
    observed_items: Iterable[dict[str, Any]],
) -> int:
    known = {
        equipment_identity_key(item): equipment_fingerprint(item)
        for item in deduplicate_items(known_items)
    }
    return sum(
        1
        for item in deduplicate_items(observed_items)
        if known.get(equipment_identity_key(item)) != equipment_fingerprint(item)
    )


def merge_equipment_snapshots(
    previous_items: Iterable[dict[str, Any]],
    fresh_items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Upsert observations and retain every previously known listing."""
    fresh = deduplicate_items(fresh_items)
    fresh_keys = {equipment_identity_key(item) for item in fresh}
    previous = deduplicate_items(previous_items)
    return fresh + [item for item in previous if equipment_identity_key(item) not in fresh_keys]
