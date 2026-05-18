{#-
    Concatena dois arrays de strings de forma compatível entre BigQuery e DuckDB.
    Substitui COALESCE de cada operando por array vazio antes de concatenar,
    evitando NULL propagation quando um dos campos ainda não foi populado.

    Uso:
        {{ array_concat_strings('mentioned_mayors', 'mentioned_governors') }}
-#}

{% macro array_concat_strings(arr1, arr2) %}
    {% if target.type == 'bigquery' %}
        ARRAY_CONCAT(
            COALESCE({{ arr1 }}, []),
            COALESCE({{ arr2 }}, [])
        )
    {% elif target.type == 'duckdb' %}
        list_concat(
            COALESCE({{ arr1 }}, []),
            COALESCE({{ arr2 }}, [])
        )
    {% else %}
        ARRAY_CONCAT(
            COALESCE({{ arr1 }}, []),
            COALESCE({{ arr2 }}, [])
        )
    {% endif %}
{% endmacro %}
