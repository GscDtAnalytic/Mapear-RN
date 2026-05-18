"""Entity resolution for canonical political targets (mayors, governor, candidates).

Provides PersonResolver: maps a raw mention (name + context + handle) to a
canonical person_id with confidence score and scope_status. Backed by the
``dbt/seeds/rn_targets.csv`` seed.
"""

from mapear_domain.entity_resolution.author_resolver import (
    IDENTITY_RESOLUTION_AUTHOR_VERSION,
    AuthorKey,
    Decision,
    PairScore,
    Persona,
    Thresholds,
    blocking_keys,
    jaro_winkler,
    normalize_display_name,
    normalize_handle,
    resolve_personas,
    score_pair,
)
from mapear_domain.entity_resolution.confidence_scorer import (
    IDENTITY_RESOLUTION_VERSION,
    ConfidenceBreakdown,
    ResolutionConfidenceScorer,
)
from mapear_domain.entity_resolution.identity_audit import (
    IdentityAuditor,
    IdentityReviewQueue,
    ReviewItem,
    ValidationViolation,
    ViolationKind,
    is_institutional_name,
    validate_handle_format,
)
from mapear_domain.entity_resolution.person_resolver import (
    PersonResolver,
    ResolutionResult,
    ScopeStatus,
    Target,
    set_targets_seed_path,
)

__all__ = [
    "IDENTITY_RESOLUTION_AUTHOR_VERSION",
    "IDENTITY_RESOLUTION_VERSION",
    "AuthorKey",
    "ConfidenceBreakdown",
    "Decision",
    "IdentityAuditor",
    "IdentityReviewQueue",
    "PairScore",
    "Persona",
    "PersonResolver",
    "ResolutionConfidenceScorer",
    "ResolutionResult",
    "ReviewItem",
    "ScopeStatus",
    "Target",
    "Thresholds",
    "ValidationViolation",
    "ViolationKind",
    "blocking_keys",
    "is_institutional_name",
    "jaro_winkler",
    "normalize_display_name",
    "normalize_handle",
    "resolve_personas",
    "score_pair",
    "set_targets_seed_path",
    "validate_handle_format",
]
