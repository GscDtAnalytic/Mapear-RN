{# Override the default dbt behavior of concatenating <target.schema>_<custom>.

   With this macro, `+schema: mapear_silver` in dbt_project.yml lands in the
   dataset `mapear_silver` directly, instead of `mapear_gold_mapear_silver`
   (since target.schema defaults to `mapear_gold` in profiles.yml prod).

   Models without a custom schema fall back to the target's default schema.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
