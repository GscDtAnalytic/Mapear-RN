{% macro unnest_json_array(relation, column, fields, key_column='content_hash') %}
    {{ return(adapter.dispatch('unnest_json_array', 'mapear_rss')(relation, column, fields, key_column)) }}
{% endmacro %}

{% macro duckdb__unnest_json_array(relation, column, fields, key_column='content_hash') %}
    SELECT
        {{ relation }}.{{ key_column }},
        {% for field in fields %}
        unnested.{{ field }}{{ "," if not loop.last else "" }}
        {% endfor %}
    FROM {{ relation }},
    LATERAL (
        SELECT
            {% for field in fields %}
            json_extract_string(elem, '$.{{ field }}') AS {{ field }}{{ "," if not loop.last else "" }}
            {% endfor %}
        FROM unnest(
            CAST({{ relation }}.{{ column }} AS JSON[])
        ) AS t(elem)
    ) AS unnested
{% endmacro %}

{% macro bigquery__unnest_json_array(relation, column, fields, key_column='content_hash') %}
    SELECT
        {{ relation }}.{{ key_column }},
        {% for field in fields %}
        elem.{{ field }}{{ "," if not loop.last else "" }}
        {% endfor %}
    FROM {{ relation }},
    UNNEST({{ relation }}.{{ column }}) AS elem
{% endmacro %}
