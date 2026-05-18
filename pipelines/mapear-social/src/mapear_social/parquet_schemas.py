"""Arrow schemas for the social warehouse tables.

Counterpart to ``mapear_storage.loaders.parquet_writer``'s article schemas.
Lives in ``mapear_social`` (not ``mapear_storage``) so the storage layer
stays free of any social import — see ``concepts/dependency-inversion``.

Each schema is generated from the matching ``TableContract`` in
``mapear_social.contracts`` via the public ``arrow_from_contract`` helper
exported by ``mapear_storage.loaders.parquet_writer``.
"""

from __future__ import annotations

from mapear_social.contracts import SOCIAL_CONTRACTS
from mapear_storage.loaders.parquet_writer import arrow_from_contract

SOCIAL_RAW_SCHEMA = arrow_from_contract(SOCIAL_CONTRACTS["raw_social_posts"])
SOCIAL_SILVER_SCHEMA = arrow_from_contract(SOCIAL_CONTRACTS["silver_social_posts"])
SOCIAL_DLQ_SCHEMA = arrow_from_contract(SOCIAL_CONTRACTS["raw_social_posts_dlq"])
SOCIAL_AUTHOR_ACTIVATIONS_SCHEMA = arrow_from_contract(
    SOCIAL_CONTRACTS["silver_author_activations"]
)
SOCIAL_AUTHOR_COMMUNITIES_SCHEMA = arrow_from_contract(
    SOCIAL_CONTRACTS["silver_author_communities"]
)
SOCIAL_AUTHOR_PERSONAS_SCHEMA = arrow_from_contract(
    SOCIAL_CONTRACTS["silver_author_personas"]
)
