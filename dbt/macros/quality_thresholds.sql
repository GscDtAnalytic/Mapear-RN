{% macro quality_thresholds() %}
{#
  Centraliza todos os thresholds de qualidade de dados do projeto.
  Lê dos vars do dbt_project.yml; usa defaults conservadores se ausentes.
  Uso: {% set t = quality_thresholds() %}  →  {{ t.temporal_cutoff }}

  Calibração Sprint 3 B4 Fase 4 (2026-05-05):
  - city_coverage_pct_{rss,social}: calibrados via Q3 (janela 5-6 dias; reavaliar 2026-05-19).
  - mayor_coverage_pct_{rss,social}: separação por source_type (Q3); legado
    `min_mayor_coverage_pct` mantido como deprecated, remover em B4 fechamento.
  - {resolution,sentiment}_confidence_stddev: regra 30% × stddev_observado em Q4
    (resolution=0.33→0.10; sentiment=0.08→0.024). Detecta queda ≥70% na dispersão.
#}
{{ return({
  "temporal_cutoff":                  var('dq_temporal_cutoff', '2025-01-01'),
  "min_city_coverage_pct_rss":        var('dq_min_city_coverage_pct_rss', 0.30),
  "min_city_coverage_pct_social":     var('dq_min_city_coverage_pct_social', 0.50),
  "min_mayor_coverage_pct_rss":       var('dq_min_mayor_coverage_pct_rss', 0.20),
  "min_mayor_coverage_pct_social":    var('dq_min_mayor_coverage_pct_social', 0.55),
  "min_mayor_coverage_pct":           var('dq_min_mayor_coverage_pct', 0.30),
  "max_entity_mentions_per_doc":      var('dq_max_entity_mentions_per_doc', 50),
  "min_rows_coverage_check":          var('dq_min_rows_coverage_check', 20),
  "min_rows_distribution_check":      var('dq_min_rows_distribution_check', 100),
  "min_resolution_confidence_stddev": var('dq_min_resolution_confidence_stddev', 0.10),
  "min_sentiment_confidence_stddev":  var('dq_min_sentiment_confidence_stddev', 0.024),
}) }}
{% endmacro %}
