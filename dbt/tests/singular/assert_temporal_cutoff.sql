-- Temporal: nenhum evento deve ter published_at anterior ao cutoff incremental.
-- Violação indica que o filtro de watermark foi ignorado ou bypassado em alguma
-- etapa do pipeline (stg_social, stg_rss watermark, ou merge incremental do dbt).
{% set t = quality_thresholds() %}

SELECT
    event_id,
    source_type,
    platform,
    published_at
FROM {{ ref('mapear_events') }}
WHERE source_type = 'social'
  AND published_at < CAST('{{ t.temporal_cutoff }}' AS TIMESTAMP)
