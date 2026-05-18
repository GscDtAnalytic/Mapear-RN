"""Generate pyarrow schemas from Pydantic models.

Counterpart to `mapear_domain.schemas.bq_codegen` — same type rules, same
nullability knobs, different output format (pyarrow.Schema instead of BQ
JSON).

This module lives in `mapear-storage` because pyarrow is a heavy native
dependency that should not be forced on `mapear-domain` (a leaf package).

Type mapping
------------
str, HttpUrl, Enum[str], Literal[str, ...]  → pa.string()
int                                          → pa.int64()
float                                        → pa.float64()
bool                                         → pa.bool_()
datetime                                     → pa.timestamp("us", tz="UTC")
BaseModel subclass                           → pa.struct([...])
list[X]                                      → pa.list_(<X>), nullable=False

Nullability
-----------
list[X]                                      → nullable=False (REPEATED convention)
X | None                                     → nullable=True
plain X                                      → nullable=False, unless...
  - permissive=True: all top-level scalars nullable
  - name in nullable_overrides: forced nullable=True

`field_order` semantics match BQ codegen — see that module's docstring.
"""

from __future__ import annotations

import types
import typing
from datetime import datetime
from enum import Enum

import pyarrow as pa
from pydantic import BaseModel, HttpUrl

_TIMESTAMP = pa.timestamp("us", tz="UTC")


def _resolve_optional(annotation: object) -> tuple[object, bool]:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    is_union = origin is typing.Union or isinstance(annotation, types.UnionType)
    if is_union and type(None) in args:
        non_none = tuple(a for a in args if a is not type(None))
        if len(non_none) == 1:
            return non_none[0], True
    return annotation, False


def _scalar_to_arrow(t: object) -> pa.DataType:
    if t is HttpUrl:
        return pa.string()
    if typing.get_origin(t) is typing.Literal:
        first = typing.get_args(t)[0]
        if isinstance(first, bool):
            return pa.bool_()
        if isinstance(first, str):
            return pa.string()
        if isinstance(first, int):
            return pa.int64()
        if isinstance(first, float):
            return pa.float64()
    if isinstance(t, type):
        if issubclass(t, bool):
            return pa.bool_()
        if issubclass(t, int):
            return pa.int64()
        if issubclass(t, float):
            return pa.float64()
        if issubclass(t, str):
            return pa.string()
        if issubclass(t, datetime):
            return _TIMESTAMP
        if issubclass(t, Enum):
            return pa.string()
    raise TypeError(f"unmapped scalar type: {t!r}")


def _is_basemodel(t: object) -> bool:
    return isinstance(t, type) and issubclass(t, BaseModel)


def _struct_for_model(model: type[BaseModel]) -> pa.DataType:
    """Build a pa.struct from a Pydantic sub-model.

    Convention: every nested struct sub-field is nullable. Mirrors the
    deployed warehouse — `_ENTITY_STRUCT`, `_SENTIMENT_STRUCT`,
    `_DECISION_FACTOR_STRUCT`, `_SOCIAL_ACCOUNT_STRUCT`, and
    `_SOCIAL_ENGAGEMENT_STRUCT` all set every sub-field to the pa.field
    default `nullable=True`. The Pydantic model still validates input at
    ingest time, so this is purely a warehouse storage permissiveness.
    """
    fields = []
    for sub_name, sub_field in model.model_fields.items():
        inner, _optional = _resolve_optional(sub_field.annotation)
        if _is_basemodel(inner):
            arrow_type = _struct_for_model(inner)
        else:
            arrow_type = _scalar_to_arrow(inner)
        fields.append(pa.field(sub_name, arrow_type, nullable=True))
    return pa.struct(fields)


def _emit_field(
    name: str,
    annotation: object,
    *,
    permissive: bool,
    nullable_overrides: frozenset[str],
) -> pa.Field:
    inner, optional = _resolve_optional(annotation)
    origin = typing.get_origin(inner)
    if origin is list:
        (item_t,) = typing.get_args(inner)
        if _is_basemodel(item_t):
            list_type = pa.list_(_struct_for_model(item_t))
        else:
            list_type = pa.list_(_scalar_to_arrow(item_t))
        # REPEATED convention: list itself is non-nullable in Arrow.
        return pa.field(name, list_type, nullable=False)

    forced = name in nullable_overrides or permissive
    nullable = optional or forced

    if _is_basemodel(inner):
        return pa.field(name, _struct_for_model(inner), nullable=nullable)
    return pa.field(name, _scalar_to_arrow(inner), nullable=nullable)


def pydantic_to_arrow(
    model: type[BaseModel],
    *,
    permissive: bool = False,
    field_order: list[str] | None = None,
    nullable_overrides: frozenset[str] | set[str] | None = None,
) -> pa.Schema:
    """Generate a pyarrow.Schema from a Pydantic model.

    See module docstring for type/nullability rules.
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

    return pa.schema(
        [
            _emit_field(
                name,
                annotations[name],
                permissive=permissive,
                nullable_overrides=nullable_overrides,
            )
            for name in ordered
        ]
    )


__all__ = ["pydantic_to_arrow"]
