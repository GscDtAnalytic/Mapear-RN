{% macro normalize_nfkc(text_col) %}
    {#-
        Unicode normalization para comparação de texto cross-platform.
        BQ:     NORMALIZE({{ text_col }}, NFKC) — compatibility + canonical decomposition.
        DuckDB: nfc_normalize({{ text_col }})  — apenas canonical (sem compatibility).

        NFC vs NFKC: NFKC decompõe adicionalmente ligaduras tipográficas (ﬁ→fi),
        expoentes (²→2), e formas de compatibilidade CJK. Para corpus em português
        brasileiro padrão (sem CJK ou ligaduras tipográficas), a diferença é desprezível:
        ℕ→N e similares não aparecem em posts de redes sociais de políticos do RN.
        O risco de FP/FN cross-environment por essa diferença é avaliado como negligível
        para o corpus atual. Reavaliar se o corpus expandir para outras línguas ou se
        testes de snapshot dev↔prod produzirem divergências em text_prefix_100.
    -#}
    {% if target.type == 'bigquery' %}
        NORMALIZE({{ text_col }}, NFKC)
    {% else %}
        nfc_normalize({{ text_col }})
    {% endif %}
{% endmacro %}
