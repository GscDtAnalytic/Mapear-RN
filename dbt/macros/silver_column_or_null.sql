{#-
    Project a column from a silver source, or NULL of the given type if the
    physical table doesn't yet have it. Used by staging models during the
    electoral pivot (BL-RESTRUCT / Fase 1) to reference columns that the
    Pydantic schema declares but that existing prod silver tables haven't
    had bq-update applied for yet.

    When `source_name`/`table_name` resolve to a relation that exists AND
    contains the column (case-insensitive), emits the bare column name
    (passes through). Otherwise emits `CAST(NULL AS <sql_type>) AS <col>`
    so the staging view compiles and runs against the legacy schema.

    On DuckDB dev, the placeholder macro in
    `create_duckdb_source_placeholders` materializes the columns, so this
    is a no-op there. On prod BigQuery, coverage depends on the BL-11 ops
    flow (`bq update --schema=<file>`) having run before the next dbt run.
-#}

{% macro silver_column_or_null(source_name, table_name, column_name, sql_type) %}
    {%- set src = source(source_name, table_name) -%}
    {%- set rel = adapter.get_relation(
            database=src.database,
            schema=src.schema,
            identifier=src.identifier
    ) -%}
    {%- if rel is none -%}
        CAST(NULL AS {{ sql_type }}) AS {{ column_name }}
    {%- else -%}
        {%- set cols = adapter.get_columns_in_relation(rel) | map(attribute='name') | map('lower') | list -%}
        {%- if column_name | lower in cols -%}
            {{ column_name }}
        {%- else -%}
            CAST(NULL AS {{ sql_type }}) AS {{ column_name }}
        {%- endif -%}
    {%- endif -%}
{% endmacro %}
