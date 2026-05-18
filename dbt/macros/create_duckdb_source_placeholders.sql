{#-
    Create empty placeholder tables for all sources on DuckDB dev/CI targets,
    so `dbt build` can materialize staging views without real ingested data.

    Columns mirror the physical schemas defined in
    mapear-core/src/mapear_core/loaders/parquet_writer.py and the BigQuery
    DDL in dbt/create_placeholder_tables.sql. Kept in sync manually — when
    a new column lands in prod, add it here too or the staging view will
    reference a missing column.

    No-op outside DuckDB so prod BigQuery runs are never touched.
-#}

{% macro create_duckdb_source_placeholders() %}
    {% if target.type != 'duckdb' %}
        {{ return('') }}
    {% endif %}

    {% set statements = [
        "CREATE TABLE IF NOT EXISTS main.raw_articles (url VARCHAR, source_feed VARCHAR, title VARCHAR, content VARCHAR, author VARCHAR, published_at TIMESTAMP, extracted_at TIMESTAMP, content_hash VARCHAR, html_lang VARCHAR, source_type VARCHAR, schema_version BIGINT)",
        "CREATE TABLE IF NOT EXISTS main.silver_articles (url VARCHAR, source_feed VARCHAR, title VARCHAR, content_clean VARCHAR, author VARCHAR, published_at TIMESTAMP, extracted_at TIMESTAMP, content_hash VARCHAR, entities VARCHAR[], mentioned_cities VARCHAR[], mentioned_mayors VARCHAR[], mentioned_governors VARCHAR[], mentioned_parties VARCHAR[], mentioned_persons VARCHAR[], is_rn_relevant BOOLEAN, source_type VARCHAR, schema_version BIGINT, person_id VARCHAR, scope_status VARCHAR, resolution_confidence DOUBLE)",
        "CREATE TABLE IF NOT EXISTS main.gold_articles (url VARCHAR, source_feed VARCHAR, title VARCHAR, content_clean VARCHAR, published_at TIMESTAMP, content_hash VARCHAR, is_rn_relevant BOOLEAN, mentioned_cities VARCHAR[], mentioned_mayors VARCHAR[], mentioned_governors VARCHAR[], mentioned_parties VARCHAR[], mentioned_persons VARCHAR[], sentiment_overall DOUBLE, sentiment_by_entity VARCHAR, topics VARCHAR[], topic_id BIGINT, topic_label VARCHAR, topic_id_source VARCHAR, topic_label_raw VARCHAR, trend_score DOUBLE, source_type VARCHAR, schema_version BIGINT, processed_at_utc TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS main.raw_social_posts_facebook (post_id VARCHAR, platform VARCHAR, url VARCHAR, account VARCHAR, text VARCHAR, language VARCHAR, published_at TIMESTAMP, extracted_at TIMESTAMP, engagement VARCHAR, is_repost BOOLEAN, is_reply BOOLEAN, parent_post_id VARCHAR, content_hash VARCHAR, actor_run_id VARCHAR, ingestion_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.raw_social_posts_instagram (post_id VARCHAR, platform VARCHAR, url VARCHAR, account VARCHAR, text VARCHAR, language VARCHAR, published_at TIMESTAMP, extracted_at TIMESTAMP, engagement VARCHAR, is_repost BOOLEAN, is_reply BOOLEAN, parent_post_id VARCHAR, content_hash VARCHAR, actor_run_id VARCHAR, ingestion_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.raw_social_posts_x (post_id VARCHAR, platform VARCHAR, url VARCHAR, account VARCHAR, text VARCHAR, language VARCHAR, published_at TIMESTAMP, extracted_at TIMESTAMP, engagement VARCHAR, is_repost BOOLEAN, is_reply BOOLEAN, parent_post_id VARCHAR, content_hash VARCHAR, actor_run_id VARCHAR, ingestion_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.raw_social_posts_tiktok (post_id VARCHAR, platform VARCHAR, url VARCHAR, account VARCHAR, text VARCHAR, language VARCHAR, published_at TIMESTAMP, extracted_at TIMESTAMP, engagement VARCHAR, is_repost BOOLEAN, is_reply BOOLEAN, parent_post_id VARCHAR, content_hash VARCHAR, actor_run_id VARCHAR, ingestion_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_social_posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, author_handle VARCHAR, author_display_name VARCHAR, author_verified BOOLEAN, text VARCHAR, language VARCHAR, language_confidence DOUBLE, language_reason VARCHAR, published_at TIMESTAMP, extracted_at TIMESTAMP, likes BIGINT, comments BIGINT, shares BIGINT, views BIGINT, is_repost BOOLEAN, is_reply BOOLEAN, parent_post_id VARCHAR, entities VARCHAR, mentioned_cities VARCHAR[], mentioned_mayors VARCHAR[], mentioned_governors VARCHAR[], mentioned_parties VARCHAR[], mentioned_persons VARCHAR[], is_rn_relevant BOOLEAN, sentiment_overall DOUBLE, sentiment_by_entity VARCHAR, person_id VARCHAR, scope_status VARCHAR, resolution_confidence DOUBLE, sentiment_label VARCHAR, confidence_score DOUBLE, risk_score DOUBLE, decision_factors VARCHAR, content_hash VARCHAR, actor_run_id VARCHAR, ingestion_run_id VARCHAR, rule_version VARCHAR, model_version VARCHAR, pipeline_version VARCHAR, source_type VARCHAR, batch_id VARCHAR, author_base_city VARCHAR, effective_cutoff_date TIMESTAMP, identity_resolution_version VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_article_stances (content_hash VARCHAR, stance_prompt_version VARCHAR, stance_label VARCHAR, stance_model VARCHAR, classified_at TIMESTAMP, pipeline_version VARCHAR, region VARCHAR, person_id VARCHAR, person_name VARCHAR, person_role VARCHAR, confidence DOUBLE, error VARCHAR, cache_hit BOOLEAN, redaction_level VARCHAR, job_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_narrative_clusters (cluster_run_date DATE, content_hash VARCHAR, algorithm VARCHAR, cluster_id BIGINT, member_role VARCHAR, cluster_size BIGINT, cluster_label VARCHAR, avg_intra_cluster_distance DOUBLE, distance_to_centroid DOUBLE, embedding_model VARCHAR, region VARCHAR, run_at TIMESTAMP, pipeline_version VARCHAR, job_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_narrative_embeddings (content_hash VARCHAR, embedding_model VARCHAR, embedding_dim BIGINT, embedding DOUBLE[], run_at TIMESTAMP, pipeline_version VARCHAR, narrative_prompt_version VARCHAR, rule_version VARCHAR, region VARCHAR, job_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_author_activations (author_id VARCHAR, author_id_raw VARCHAR, platform VARCHAR, content_hash VARCHAR, person_target VARCHAR, person_target_raw VARCHAR, target_kind VARCHAR, target_person_id VARCHAR, published_at TIMESTAMP, extracted_at TIMESTAMP, post_id VARCHAR, author_in_scope BOOLEAN, region VARCHAR, batch_id VARCHAR, actor_run_id VARCHAR, ingestion_run_id VARCHAR, pipeline_version VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_author_communities (activation_date DATE, author_id VARCHAR, author_id_raw VARCHAR, platform VARCHAR, algorithm VARCHAR, community_id BIGINT, community_size BIGINT, avg_co_post_count DOUBLE, avg_jaccard DOUBLE, edge_count BIGINT, edge_density DOUBLE, region VARCHAR, run_at TIMESTAMP, pipeline_version VARCHAR, job_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_author_personas (persona_id VARCHAR, platform VARCHAR, author_id VARCHAR, author_id_raw VARCHAR, member_count BIGINT, canonical_handle VARCHAR, canonical_handle_raw VARCHAR, canonical_display_name VARCHAR, confidence DOUBLE, resolution_version VARCHAR, activation_date DATE, evidence_json VARCHAR, region VARCHAR, run_at TIMESTAMP, pipeline_version VARCHAR, job_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_cluster_series (activation_date DATE, algorithm VARCHAR, community_id BIGINT, series_id VARCHAR, is_new_series BOOLEAN, jaccard_to_previous DOUBLE, series_start_date DATE, region VARCHAR, run_at TIMESTAMP, pipeline_version VARCHAR, job_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_community_scores (activation_date DATE, algorithm VARCHAR, community_id BIGINT, composite_score DOUBLE, score_version VARCHAR, community_size BIGINT, pair_count BIGINT, avg_synchrony_score DOUBLE, avg_alignment_score DOUBLE, avg_content_similarity_score DOUBLE, score_weights_json VARCHAR, region VARCHAR, run_at TIMESTAMP, pipeline_version VARCHAR, job_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_social_post_embeddings (content_hash VARCHAR, embedding_model VARCHAR, embedding_dim BIGINT, embedding DOUBLE[], run_at TIMESTAMP, pipeline_version VARCHAR, region VARCHAR, job_run_id VARCHAR, schema_version BIGINT, source_type VARCHAR)",
        "CREATE TABLE IF NOT EXISTS main.silver_event_shadow (content_hash VARCHAR, shadow_rule_version VARCHAR, primary_rule_version VARCHAR, polarity DOUBLE, volume_24h BIGINT, velocity DOUBLE, engagement BIGINT, recurrence DOUBLE, primary_label VARCHAR, primary_confidence DOUBLE, primary_risk_score DOUBLE, shadow_label VARCHAR, shadow_confidence DOUBLE, shadow_risk_score DOUBLE, shadow_decision_factors VARCHAR, person_id VARCHAR, source_type VARCHAR, region VARCHAR, tenant_id VARCHAR, pipeline_version VARCHAR, model_version VARCHAR, actor_run_id VARCHAR, ingestion_run_id VARCHAR, processed_at_utc TIMESTAMP, schema_version BIGINT)",
        "CREATE TABLE IF NOT EXISTS main.silver_mayor_endorsements (mayor_id VARCHAR, mayor_name VARCHAR, mayor_party VARCHAR, endorsement_prompt_version VARCHAR, detected_candidate VARCHAR, confidence VARCHAR, rationale VARCHAR, evidence_ids VARCHAR[], endorsement_model VARCHAR, article_count BIGINT, cache_hit BOOLEAN, error VARCHAR, redaction_level VARCHAR, investigated_at TIMESTAMP, job_run_id VARCHAR, pipeline_version VARCHAR, schema_version BIGINT, region VARCHAR, tenant_id VARCHAR)"
    ] %}

    {#-
        Schema drift — idempotent ADD COLUMN IF NOT EXISTS. Without this,
        a placeholder created by an earlier run keeps its old schema
        (CREATE TABLE IF NOT EXISTS is a no-op on existing tables) and
        staging views fail when the model projects a new column.
        BL-RESTRUCT Fase 1 added person_id/scope_status/resolution_confidence
        to silver_articles.
    -#}
    {% set drift_statements = [
        "ALTER TABLE main.silver_articles ADD COLUMN IF NOT EXISTS person_id VARCHAR",
        "ALTER TABLE main.silver_articles ADD COLUMN IF NOT EXISTS scope_status VARCHAR",
        "ALTER TABLE main.silver_articles ADD COLUMN IF NOT EXISTS resolution_confidence DOUBLE",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS language_confidence DOUBLE",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS language_reason VARCHAR",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS author_base_city VARCHAR",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS effective_cutoff_date TIMESTAMP",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS identity_resolution_version VARCHAR",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS mentioned_candidates VARCHAR[]",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS mentioned_politicians VARCHAR[]",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS topic_id_source VARCHAR",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS topic_label_raw VARCHAR",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS processed_at_utc TIMESTAMP",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS sentiment_label VARCHAR",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS confidence_score DOUBLE",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS risk_score DOUBLE",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS decision_factors VARCHAR",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS rule_version VARCHAR",
        "ALTER TABLE main.gold_articles ADD COLUMN IF NOT EXISTS model_version VARCHAR",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS narrative_summary VARCHAR",
        "ALTER TABLE main.silver_social_posts ADD COLUMN IF NOT EXISTS narrative_prompt_version VARCHAR",
        "ALTER TABLE main.silver_article_stances ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_narrative_clusters ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_narrative_embeddings ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_author_activations ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_author_communities ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_author_personas ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_cluster_series ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_community_scores ADD COLUMN IF NOT EXISTS tenant_id VARCHAR",
        "ALTER TABLE main.silver_social_post_embeddings ADD COLUMN IF NOT EXISTS tenant_id VARCHAR"
    ] %}

    {% for sql in statements %}
        {% do run_query(sql) %}
    {% endfor %}

    {% for sql in drift_statements %}
        {% do run_query(sql) %}
    {% endfor %}

    {{ return('') }}
{% endmacro %}
