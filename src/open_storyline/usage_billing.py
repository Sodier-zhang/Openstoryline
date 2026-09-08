from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List


USAGE_FILENAME = "llm_usage.json"


def usage_file_path(outputs_dir: str | Path, session_id: str) -> Path:
    return Path(outputs_dir) / str(session_id) / USAGE_FILENAME


def append_usage_record(outputs_dir: str | Path, session_id: str, record: Dict[str, Any]) -> None:
    path = usage_file_path(outputs_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    data: Dict[str, Any] = {"records": []}
    if path.exists() and path.stat().st_size > 0:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("records"), list):
                data = loaded
        except Exception:
            data = {"records": []}

    rec = dict(record)
    rec.setdefault("id", uuid.uuid4().hex)
    rec.setdefault("created_at", time.time())
    data["records"].append(rec)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def per_call_billing_record(
    *,
    model: str,
    billing_cfg: Any,
    request_id: str = "",
    node_id: str = "",
    node_name: str = "",
    node_kind: str = "",
    artifact_id: str = "",
    modality: str = "",
) -> Dict[str, Any] | None:
    if billing_cfg is None or not bool(getattr(billing_cfg, "enabled", True)):
        return None

    model_name = str(model or "").strip()
    if not model_name:
        return None

    per_call_costs = getattr(billing_cfg, "per_call_costs", None) or {}
    cost_cfg = _lookup_model_config(per_call_costs, model_name)
    if not cost_cfg:
        return None

    amount = _float_or_none(cost_cfg.get("amount") or cost_cfg.get("cost"))
    if amount is None or amount <= 0:
        return None

    return {
        "source": "provider_billing",
        "billing_source": "per_call_cost_config",
        "request_id": str(request_id or ""),
        "model": model_name,
        "amount": amount,
        "currency": str(cost_cfg.get("currency") or getattr(billing_cfg, "currency", "USD") or "USD"),
        "node_id": str(node_id or ""),
        "node_name": str(node_name or ""),
        "node_kind": str(node_kind or ""),
        "artifact_id": str(artifact_id or ""),
        "modality": str(modality or ""),
    }


def read_usage_summary(
    outputs_dir: str | Path,
    session_id: str,
    *,
    currency: str = "USD",
    billing_cfg: Any = None,
    model_api_keys: Dict[str, str] | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> Dict[str, Any]:
    path = usage_file_path(outputs_dir, session_id)
    records = []
    if path.exists() and path.stat().st_size > 0:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("records"), list):
                records = [r for r in loaded["records"] if isinstance(r, dict)]
        except Exception:
            records = []

    actual_records = []
    total_cost = 0.0
    resolved_currency = currency or "USD"
    request_context = _request_context_by_id(records)

    for rec in records:
        if rec.get("source") != "provider_billing":
            continue

        public_rec = _public_billing_record(rec, billing_cfg=billing_cfg)
        if not public_rec:
            continue
        actual_records.append(_attach_request_context(public_rec, request_context))

    for rec in actual_records:
        amount = _float_or_none(rec.get("amount"))
        if amount is None:
            continue
        total_cost += amount
        resolved_currency = str(rec.get("currency") or resolved_currency or "USD")

    if not actual_records:
        message = (
            "No actual provider billing records are available for this task. "
            "Billing is grouped by model only from real provider_billing records."
        )
        return {
            "available": False,
            "currency": resolved_currency or "USD",
            "total_cost": None,
            "by_model": _empty_model_summary(model_api_keys, resolved_currency or "USD"),
            "records": [],
            "message": message,
        }

    result = {
        "available": True,
        "currency": resolved_currency or "USD",
        "total_cost": round(total_cost, 8),
        "by_model": _summarize_by_model(
            actual_records,
            resolved_currency or "USD",
            configured_models=(model_api_keys or {}).keys(),
        ),
        "records": actual_records,
    }
    return result


def _public_billing_record(
    rec: Dict[str, Any],
    *,
    billing_cfg: Any = None,
    default_currency: str | None = None,
) -> Dict[str, Any] | None:
    amount_field = getattr(billing_cfg, "amount_field", "amount") if billing_cfg is not None else "amount"
    currency_field = getattr(billing_cfg, "currency_field", "currency") if billing_cfg is not None else "currency"
    request_id_field = getattr(billing_cfg, "request_id_field", "request_id") if billing_cfg is not None else "request_id"

    amount = _float_or_none(_field_value(rec, amount_field))
    if amount is None:
        amount = _float_or_none(rec.get("actual_cost"))
    if amount is None:
        amount = _float_or_none(rec.get("cost"))
    if amount is None:
        return None

    public_rec = {
        k: v
        for k, v in rec.items()
        if k
        not in {
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "prompt_tokens",
            "completion_tokens",
            "estimated",
            "input_per_1m",
            "output_per_1m",
            "pricing_configured",
        }
    }
    public_rec["source"] = "provider_billing"
    public_rec["amount"] = amount
    public_rec["currency"] = _field_value(rec, currency_field) or default_currency or rec.get("currency") or "USD"
    request_id = _field_value(rec, request_id_field) or rec.get("request_id")
    if request_id:
        public_rec["request_id"] = str(request_id)
    return public_rec


def _request_context_by_id(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    context = {}
    for rec in records:
        if rec.get("source") not in {"llm_request", "model_request"}:
            continue
        request_id = str(rec.get("request_id") or "").strip()
        if not request_id:
            continue
        context[request_id] = {
            k: rec.get(k)
            for k in ("node_id", "node_name", "node_kind", "artifact_id", "model", "modality")
            if rec.get(k) is not None
        }
    return context


def _summarize_by_model(
    records: List[Dict[str, Any]],
    default_currency: str,
    *,
    configured_models=None,
) -> Dict[str, Dict[str, Any]]:
    by_model: Dict[str, Dict[str, Any]] = {}
    for model in configured_models or []:
        model_s = str(model or "").strip()
        if model_s:
            by_model[model_s] = {
                "currency": default_currency or "USD",
                "total_cost": 0.0,
                "count": 0,
            }
    for rec in records:
        model = str(rec.get("model") or "unknown")
        amount = _float_or_none(rec.get("amount"))
        if amount is None:
            continue
        entry = by_model.setdefault(
            model,
            {
                "currency": str(rec.get("currency") or default_currency or "USD"),
                "total_cost": 0.0,
                "count": 0,
            },
        )
        entry["total_cost"] += amount
        entry["count"] += 1
    for entry in by_model.values():
        entry["total_cost"] = round(float(entry["total_cost"]), 8)
    return by_model


def _empty_model_summary(
    model_api_keys: Dict[str, str] | None,
    default_currency: str,
) -> Dict[str, Dict[str, Any]]:
    return {
        str(model): {
            "currency": default_currency or "USD",
            "total_cost": None,
            "count": 0,
        }
        for model in (model_api_keys or {}).keys()
    }


def _lookup_model_config(configs: Dict[str, Any], model: str) -> Dict[str, Any] | None:
    if not isinstance(configs, dict):
        return None
    if model in configs and isinstance(configs[model], dict):
        return configs[model]

    model_l = model.lower()
    for key, value in configs.items():
        if str(key).lower() == model_l and isinstance(value, dict):
            return value
    return None


def _attach_request_context(
    rec: Dict[str, Any],
    request_context: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    request_id = str(rec.get("request_id") or "").strip()
    if not request_id:
        return rec
    ctx = request_context.get(request_id)
    if not ctx:
        return rec
    merged = dict(rec)
    for key, value in ctx.items():
        merged.setdefault(key, value)
    return merged


def _dig_path(data: Any, path: str) -> Any:
    cur = data
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
            continue
        return None
    return cur


def _field_value(data: Dict[str, Any], field: str) -> Any:
    if not isinstance(data, dict):
        return None
    if field in data:
        return data.get(field)
    return _dig_path(data, field)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None
