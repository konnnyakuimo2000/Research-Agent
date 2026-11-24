"""依頼の設定を、エージェントの名乗りの settings_schema（JSON Schema の一部）で確かめる。

画面の設定の欄は settings_schema から自動で作る（docs/telemetry_enhancement_plan.md D-1）。ゲートウェイは、
エージェントへ送る前に同じ形で確かめ、知らない名前・型の違う値・範囲の外の値を断る（400）。
扱うのは、設定の欄が作れる形だけ: integer・number・boolean・string（enum も）と、enum の配列。
"""

from __future__ import annotations


class SettingsError(ValueError):
    pass


def check_settings(schema: dict, values: dict) -> dict:
    """確かめた設定を返す（既定の値は足さない。エージェントが自分の既定を使う）。"""
    props = (schema or {}).get("properties") or {}
    out = {}
    for name, value in (values or {}).items():
        spec = props.get(name)
        if spec is None:
            raise SettingsError(f"このエージェントには「{name}」という設定がありません")
        out[name] = _check(spec.get("title") or name, spec, value)
    return out


def _check(title: str, spec: dict, value):
    kind = spec.get("type")
    if kind == "boolean":
        if not isinstance(value, bool):
            raise SettingsError(f"{title} は、はい・いいえで指定してください")
        return value
    if kind in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "integer" and value != int(value)):
            raise SettingsError(f"{title} は{'整数' if kind == 'integer' else '数'}で指定してください")
        value = int(value) if kind == "integer" else value
        lo, hi = spec.get("minimum"), spec.get("maximum")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise SettingsError(f"{title} は {lo if lo is not None else ''}〜{hi if hi is not None else ''} の範囲で指定してください")
        return value
    if kind == "string":
        if not isinstance(value, str):
            raise SettingsError(f"{title} は文字で指定してください")
        if "enum" in spec and value not in spec["enum"]:
            raise SettingsError(f"{title} は {'・'.join(map(str, spec['enum']))} のどれかにしてください")
        return value
    if kind == "array":
        allowed = (spec.get("items") or {}).get("enum")
        if not isinstance(value, list) or (allowed is not None and any(v not in allowed for v in value)):
            raise SettingsError(f"{title} は {'・'.join(map(str, allowed or []))} から選んでください")
        return list(value)
    raise SettingsError(f"{title} は画面から指定できない形の設定です")
