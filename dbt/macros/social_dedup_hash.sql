{% macro stable_hash(input) %}
    {#-
        SHA256 hex digest — cross-environment determinism.
        BQ:     TO_HEX(SHA256(expr))  → BYTES→VARCHAR hex via native function.
        DuckDB: sha256(expr)           → VARCHAR hex directly (same output).
        Both return the same 64-char lowercase hex string for identical input.
        Note: lower(hex(sha256())) in DuckDB would double-encode (hex of hex).
    -#}
    {% if target.type == 'bigquery' %}
        TO_HEX(SHA256({{ input }}))
    {% else %}
        sha256({{ input }})
    {% endif %}
{% endmacro %}
