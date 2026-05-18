{% macro list_contains(list_column, value_column) %}
    {{ return(adapter.dispatch('list_contains', 'mapear_rss')(list_column, value_column)) }}
{% endmacro %}

{% macro duckdb__list_contains(list_column, value_column) %}
    list_contains(CAST({{ list_column }} AS VARCHAR[]), {{ value_column }})
{% endmacro %}

{% macro bigquery__list_contains(list_column, value_column) %}
    {{ value_column }} IN UNNEST({{ list_column }})
{% endmacro %}
