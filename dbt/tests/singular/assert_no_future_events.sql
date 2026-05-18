-- INV-003: published_at não deve estar no futuro.
-- Buffer de 1 hora para diferenças de fuso horário. Violação indica dado corrompido,
-- bug de parsing de data, ou ingestão indevida de conteúdo agendado/pré-datado.
--
-- Cross-dialect: dbt.dateadd em BQ casta para DATETIME (datetime_add(cast as datetime)),
-- causando mismatch TIMESTAMP > DATETIME. Aqui usamos timestamp_add direto em BQ
-- e dbt.dateadd em DuckDB. Mesmo padrão usado em int_social_posts__deduped.sql.
SELECT
    event_id,
    source_type,
    platform,
    published_at
FROM {{ ref('mapear_events') }}
WHERE published_at >
    {%- if target.type == 'bigquery' %}
        TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
    {%- else %}
        {{ dbt.dateadd(datepart='hour', interval=1, from_date_or_timestamp=dbt.current_timestamp()) }}
    {%- endif %}
