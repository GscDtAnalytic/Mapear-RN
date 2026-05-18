"""Drift detector: _CRITICAL_FIELDS_SOCIAL must stay in sync with SocialPost model.

If a field in _CRITICAL_FIELDS_SOCIAL is renamed/removed in the Pydantic model,
this test fails fast — preventing silent quality checker breakage.
"""

import pytest

from mapear_infra.quality import _CRITICAL_FIELDS_SOCIAL
from mapear_social.models import SocialPost


def _social_post_fields() -> set[str]:
    return set(SocialPost.model_fields.keys())


@pytest.mark.parametrize("field", sorted(_CRITICAL_FIELDS_SOCIAL))
def test_critical_social_field_exists_in_pydantic_model(field: str) -> None:
    """Each field in _CRITICAL_FIELDS_SOCIAL must exist in SocialPost."""
    assert field in _social_post_fields(), (
        f"Field '{field}' is in _CRITICAL_FIELDS_SOCIAL but not in SocialPost. "
        "Update _CRITICAL_FIELDS_SOCIAL in mapear-infra/src/mapear_infra/quality.py "
        "to match the current model. See docs/sprint3_b6_critical_fields_social.md."
    )
