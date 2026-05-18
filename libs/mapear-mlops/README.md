# mapear-mlops

[← voltar para README raiz](../../README.md)

MLflow para tracking de avaliações de NLP e baselines. **Não treina modelos** — registra qualidade do que já existe.

---

## O que é

Camada de suporte para o ciclo de avaliação contínua dos componentes determinísticos de NLP. Cada execução do golden set (em `libs/mapear-nlp/eval/`) é registrada como um run MLflow, com:

- Métricas (precision, recall, F1) por categoria de entidade, por tipo de sentimento, por tópico.
- Parâmetros (versão do gazetteer, threshold, regras ativas).
- Artefatos (matriz de confusão, lista de falsos positivos/negativos).

## Por que existe (e por que não treinamos modelos)

O projeto **não treina modelos próprios**. Não tem dataset rotulado em escala suficiente, e os baselines determinísticos atuais resolvem o caso de uso. Mas **medimos qualidade rigorosamente** — e medir requer infraestrutura.

MLflow aqui não é "model registry no sentido clássico"; é **"qualidade-tracking-as-code"**:

- Cada PR que mexe em regras de NLP roda o golden set e compara com o baseline anterior.
- Regressões em métricas críticas (ex.: recall de PERSON < baseline - 2%) falham o CI.
- Histórico de baselines fica versionado e consultável.

## Stack

| Tecnologia | Versão | Para quê |
|---|---|---|
| **mlflow-skinny** | 2.16 | Tracking server lightweight (sem UI bundled — usamos a oficial via Docker quando necessário). |
| **loguru** | 0.7 | Logging consistente com o resto do workspace. |

Por que `mlflow-skinny` e não `mlflow` cheio? Não precisamos do model registry nem da UI no runtime do CI — só do tracking. Dependências menores, container menor.

## API pública (essencial)

```python
from mapear_mlops.tracking import EvaluationRun

with EvaluationRun(experiment="ner_baseline") as run:
    run.log_params({"gazetteer_version": "v3", "threshold": 0.8})
    run.log_metric("ner_person_f1", 0.91)
    run.log_metric("ner_person_recall", 0.88)
    run.log_artifact("eval/confusion_matrix.png")
```

Em CI, um helper compara o run atual contra a baseline mais recente do mesmo experiment e falha se houver regressão.

## Decisões locais

1. **Sem treinamento.** O nome é "mlops" por convenção, mas o foco é avaliação e tracking, não treino.
2. **Baselines medidos, não declarados.** Os números no [`docs/sprint3_b4_baseline_plan.md`](../../docs/sprint3_b4_baseline_plan.md) vêm de runs reais, não de "achismo".
3. **Tracking server local ou GCS.** Em dev, MLflow grava em `mlruns/` local; em CI, em um bucket GCS dedicado. Não pagamos por managed MLflow.

## Como testar

```bash
uv run pytest libs/mapear-mlops/tests/
```

## Estrutura

```
mapear-mlops/
├── src/mapear_mlops/
│   ├── tracking.py            # EvaluationRun wrapper
│   ├── baselines.py           # Comparação contra baseline anterior
│   └── reporting.py           # Geração de relatórios markdown
└── tests/
```
