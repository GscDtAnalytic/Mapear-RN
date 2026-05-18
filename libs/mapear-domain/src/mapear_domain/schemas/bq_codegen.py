"""Generate BigQuery table schemas from Pydantic models.

Pure-Python: no pyarrow dependency, so this module can live in
`mapear-domain` (a leaf package). The Arrow analogue lives in
`mapear-storage` because pyarrow is a heavy native dep.

Type mapping
------------
str, HttpUrl, Enum[str], Literal[str, ...]  → STRING
int                                          → INT64
float                                        → FLOAT64
bool                                         → BOOL
datetime                                     → TIMESTAMP
BaseModel subclass                           → RECORD (nested fields)
list[X]                                      → REPEATED <X>

Mode
----
list[X]                                      → REPEATED
X | None  (or Optional[X])                   → NULLABLE
plain X                                      → REQUIRED, unless...
  - permissive=True (raw layer): all top-level scalars become NULLABLE
  - name in nullable_overrides: forced NULLABLE

The `nullable_overrides` knob exists because some columns (e.g. the V1
canonical computed fields `content_rn_relevant`, `author_in_scope`) must
accept NULL to support historic rows even though the Pydantic side has
no Optional declaration to express that.

`field_order` lets callers fix the emission order when the implicit
Pydantic order (`model_fields` then `model_computed_fields`) does not
match the warehouse table — see `SilverArticle`, where the V1 computed
fields land between `resolution_confidence` and `actor_run_id` in the
deployed schema.
"""

from __future__ import annotations

import types
import typing
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, HttpUrl


def _resolve_optional(annotation: object) -> tuple[object, bool]:
    """Return (inner_type, is_optional) for ``X | None`` / ``Optional[X]``.

    For non-Optional annotations returns ``(annotation, False)``.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    is_union = origin is typing.Union or isinstance(annotation, types.UnionType)
    if is_union and type(None) in args:
        non_none = tuple(a for a in args if a is not type(None))
        if len(non_none) == 1:
            return non_none[0], True
    return annotation, False


def _scalar_to_bq_type(t: object) -> str:
    if t is HttpUrl:
        return "STRING"
    if typing.get_origin(t) is typing.Literal:
        first = typing.get_args(t)[0]
        if isinstance(first, bool):
            return "BOOL"
        if isinstance(first, str):
            return "STRING"
        if isinstance(first, int):
            return "INT64"
        if isinstance(first, float):
            return "FLOAT64"
    if isinstance(t, type):
        if issubclass(t, bool):
            return "BOOL"
        if issubclass(t, int):
            return "INT64"
        if issubclass(t, float):
            return "FLOAT64"
        if issubclass(t, str):
            return "STRING"
        if issubclass(t, datetime):
            return "TIMESTAMP"
        if issubclass(t, Enum):
            return "STRING"
    raise TypeError(f"unmapped scalar type: {t!r}")


def _is_basemodel(t: object) -> bool:
    return isinstance(t, type) and issubclass(t, BaseModel)


def _emit_struct_subfield(name: str, annotation: object) -> dict:
    """Emit a sub-field inside a RECORD. Convention: always NULLABLE.

    Mirrors the deployed warehouse, where every sub-field of every
    nested RECORD (entities, sentiment_by_entity, decision_factors,
    account, engagement) is NULLABLE. Pydantic still validates input
    at ingest — this is purely a warehouse storage permissiveness.
    """
    inner, _optional = _resolve_optional(annotation)
    origin = typing.get_origin(inner)
    if origin is list:
        (item_t,) = typing.get_args(inner)
        if _is_basemodel(item_t):
            return {
                "name": name,
                "type": "RECORD",
                "mode": "REPEATED",
                "fields": [
                    _emit_struct_subfield(sub_name, sub_field.annotation)
                    for sub_name, sub_field in item_t.model_fields.items()
                ],
            }
        return {"name": name, "type": _scalar_to_bq_type(item_t), "mode": "REPEATED"}
    if _is_basemodel(inner):
        return {
            "name": name,
            "type": "RECORD",
            "mode": "NULLABLE",
            "fields": [
                _emit_struct_subfield(sub_name, sub_field.annotation)
                for sub_name, sub_field in inner.model_fields.items()
            ],
        }
    return {"name": name, "type": _scalar_to_bq_type(inner), "mode": "NULLABLE"}


def _emit(
    name: str,
    annotation: object,
    *,
    permissive: bool,
    nullable_overrides: frozenset[str],
) -> dict:
    inner, optional = _resolve_optional(annotation)
    origin = typing.get_origin(inner)
    if origin is list:
        (item_t,) = typing.get_args(inner)
        if _is_basemodel(item_t):
            return {
                "name": name,
                "type": "RECORD",
                "mode": "REPEATED",
                "fields": [
                    _emit_struct_subfield(sub_name, sub_field.annotation)
                    for sub_name, sub_field in item_t.model_fields.items()
                ],
            }
        return {"name": name, "type": _scalar_to_bq_type(item_t), "mode": "REPEATED"}

    forced_nullable = name in nullable_overrides or permissive
    mode = "NULLABLE" if (optional or forced_nullable) else "REQUIRED"

    if _is_basemodel(inner):
        return {
            "name": name,
            "type": "RECORD",
            "mode": mode,
            "fields": [
                _emit_struct_subfield(sub_name, sub_field.annotation)
                for sub_name, sub_field in inner.model_fields.items()
            ],
        }
    return {"name": name, "type": _scalar_to_bq_type(inner), "mode": mode}


def pydantic_to_bq_json(
    model: type[BaseModel],
    *,
    permissive: bool = False,
    field_order: list[str] | None = None,
    nullable_overrides: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """Generate a BigQuery JSON schema (list of column dicts) from a Pydantic model.

    See module docstring for type/mode rules.
    """
    nullable_overrides = frozenset(nullable_overrides or ())

    annotations: dict[str, object] = {}
    for name, f in model.model_fields.items():
        annotations[name] = f.annotation
    for name, cf in model.model_computed_fields.items():
        annotations[name] = cf.return_type

    if field_order is None:
        ordered = list(annotations)
    else:
        missing = set(annotations) - set(field_order)
        if missing:
            raise ValueError(
                f"field_order missing fields for {model.__name__}: {sorted(missing)}"
            )
        extra = set(field_order) - set(annotations)
        if extra:
            raise ValueError(
                f"field_order has unknown fields for {model.__name__}: {sorted(extra)}"
            )
        ordered = list(field_order)

    return [
        _emit(
            name,
            annotations[name],
            permissive=permissive,
            nullable_overrides=nullable_overrides,
        )
        for name in ordered
    ]


__all__ = ["pydantic_to_bq_json"]
