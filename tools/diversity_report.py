"""Relatório de diversidade editorial — antes e depois da expansão de fontes.

Consulta url_frontier para calcular métricas de concentração por data e
projeta o impacto das 7 novas fontes adicionadas em 2026-04.

Uso:
    poetry run python scripts/diversity_report.py [--days N]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text

from mapear_infra.config import get_settings

# Novas fontes adicionadas na expansão de 2026-04
NEW_FEEDS: list[tuple[str, str]] = [
    ("Novo Jornal RN", "https://novojornal.jor.br/feed/"),
    ("Portal No Ar", "https://portalnoar.com.br/feed/"),
    ("Mossoró Hoje", "https://mossoro.news/feed/"),
    ("Foco RN", "https://focorn.com.br/feed/"),
    ("Seridó Online", "https://seridoonline.com.br/feed/"),
    ("RN em Pauta", "https://rnempauta.com.br/feed/"),
    ("Pontal FM", "https://pontalfm.com.br/feed/"),
]

# Estimativa conservadora de artigos/dia por nova fonte
PROJECTED_ARTICLES_PER_NEW_FEED: int = 8


def _query_distribution(engine, days: int) -> dict[str, dict[str, int]]:
    """Return {date_str: {source_feed_url: count}} for the last N days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    DATE(discovered_at AT TIME ZONE 'UTC') AS dt,
                    source_feed,
                    COUNT(*) AS cnt
                FROM url_frontier
                WHERE discovered_at >= :cutoff
                  AND status = 'completed'
                GROUP BY dt, source_feed
                ORDER BY dt DESC, cnt DESC
            """
            ),
            {"cutoff": cutoff},
        ).fetchall()

    by_date: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        by_date[str(row.dt)][row.source_feed] = row.cnt
    return dict(by_date)


def _day_metrics(dist: dict[str, int]) -> dict:
    if not dist:
        return {
            "total": 0,
            "sources": 0,
            "hhi": 0.0,
            "dominant": "-",
            "dominant_pct": 0.0,
        }
    total = sum(dist.values())
    hhi = sum((c / total) ** 2 for c in dist.values())
    dominant_url = max(dist, key=lambda k: dist[k])
    return {
        "total": total,
        "sources": len(dist),
        "hhi": round(hhi, 4),
        "dominant": dominant_url,
        "dominant_pct": round(dist[dominant_url] / total * 100, 1),
    }


def _project_after(dist: dict[str, int]) -> dict[str, int]:
    after = dict(dist)
    for _, url in NEW_FEEDS:
        after[url] = PROJECTED_ARTICLES_PER_NEW_FEED
    return after


def _short_name(url: str) -> str:
    """Return a readable short name from a feed URL."""
    return url.replace("https://", "").replace("http://", "").split("/")[0]


def _print_separator(width: int = 72) -> None:
    print("─" * width)


def main(days: int = 7) -> None:
    settings = get_settings()
    engine = create_engine(settings.postgres.dsn)

    print()
    print("=" * 72)
    print("  MAPEAR-RN — RELATÓRIO DE DIVERSIDADE EDITORIAL")
    print(f"  Gerado em: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 72)

    by_date = _query_distribution(engine, days=days)

    if not by_date:
        print(
            f"\n  [AVISO] Nenhum dado encontrado em url_frontier "
            f"(últimos {days} dias).\n"
            "  Execute o pipeline primeiro para gerar dados históricos.\n"
        )
        _print_simulated_report()
        return

    # --- Estado atual ---
    print(f"\n  ESTADO ATUAL  (últimos {days} dias)\n")
    header = f"  {'Data':<12} {'Total':>6} {'Fontes':>7} {'HHI':>7}  {'Dominante':<32} {'%':>6}"
    print(header)
    _print_separator()

    day_stats: list[dict] = []
    for date_str in sorted(by_date.keys(), reverse=True):
        dist = by_date[date_str]
        m = _day_metrics(dist)
        dom = _short_name(m["dominant"])[:30]
        print(
            f"  {date_str:<12} {m['total']:>6} {m['sources']:>7} "
            f"{m['hhi']:>7.4f}  {dom:<32} {m['dominant_pct']:>5.1f}%"
        )
        day_stats.append(m)

    if day_stats:
        avg_hhi = sum(m["hhi"] for m in day_stats) / len(day_stats)
        avg_src = sum(m["sources"] for m in day_stats) / len(day_stats)
        print(f"\n  Média 7d → HHI: {avg_hhi:.4f}   Fontes/dia: {avg_src:.1f}")

    # --- Projeção após novas fontes ---
    print(f"\n\n  PROJEÇÃO APÓS +{len(NEW_FEEDS)} NOVAS FONTES\n")

    latest_date = max(by_date.keys())
    before = _day_metrics(by_date[latest_date])
    after_dist = _project_after(by_date[latest_date])
    after = _day_metrics(after_dist)

    delta_hhi = after["hhi"] - before["hhi"]
    delta_src = after["sources"] - before["sources"]
    delta_dom = after["dominant_pct"] - before["dominant_pct"]
    delta_total = after["total"] - before["total"]

    col1, col2, col3, col4 = 36, 11, 11, 9
    print(f"  {'Métrica':<{col1}} {'ANTES':>{col2}} {'DEPOIS':>{col3}} {'Δ':>{col4}}")
    _print_separator()
    print(
        f"  {'HHI  (↓ = mais diverso)':<{col1}} "
        f"{before['hhi']:>{col2}.4f} {after['hhi']:>{col3}.4f} "
        f"{delta_hhi:>+{col4}.4f}"
    )
    print(
        f"  {'Fontes únicas ativas':<{col1}} "
        f"{before['sources']:>{col2}} {after['sources']:>{col3}} "
        f"{delta_src:>+{col4}}"
    )
    print(
        f"  {'Dominante  % do batch':<{col1}} "
        f"{before['dominant_pct']:>{col2}.1f}% {after['dominant_pct']:>{col3}.1f}% "
        f"{delta_dom:>+{col4-1}.1f}%"
    )
    print(
        f"  {'Artigos/dia (estimado)':<{col1}} "
        f"{before['total']:>{col2}} {after['total']:>{col3}} "
        f"{delta_total:>+{col4}}"
    )

    print(f"\n\n  NOVAS FONTES ADICIONADAS ({len(NEW_FEEDS)} portais)\n")
    for name, url in NEW_FEEDS:
        print(f"    + {name:<22}  {url}")

    print(
        f"\n  Projeção: {PROJECTED_ARTICLES_PER_NEW_FEED} artigos/dia "
        "por nova fonte (estimativa conservadora)"
    )
    print("\n" + "=" * 72)
    print()


def _print_simulated_report() -> None:
    """Fallback: simulated before/after with representative proportions."""
    # Proportions based on typical RN portal volumes
    sim_before: dict[str, int] = {
        "https://tribunadonorte.com.br/feed/": 42,
        "https://agorarn.com.br/feed/": 28,
        "https://g1.globo.com/rss/g1/rn/rio-grande-do-norte/": 18,
        "https://saibamais.jor.br/feed/": 14,
        "https://www.novonoticias.com.br/feed/": 11,
        "https://blogdobg.com.br/feed/": 9,
        "https://opotengi.com.br/feed/": 7,
        "https://opoti.com.br/feed/": 6,
    }
    before = _day_metrics(sim_before)
    after = _day_metrics(_project_after(sim_before))

    print(
        "\n  SIMULAÇÃO  (sem dados reais — execute o pipeline para dados históricos)\n"
    )
    col1, col2, col3 = 36, 11, 11
    print(f"  {'Métrica':<{col1}} {'ANTES':>{col2}} {'DEPOIS':>{col3}}")
    _print_separator()
    print(
        f"  {'HHI  (↓ = mais diverso)':<{col1}} "
        f"{before['hhi']:>{col2}.4f} {after['hhi']:>{col3}.4f}"
    )
    print(
        f"  {'Fontes únicas ativas':<{col1}} "
        f"{before['sources']:>{col2}} {after['sources']:>{col3}}"
    )
    print(
        f"  {'Dominante % do batch':<{col1}} "
        f"{before['dominant_pct']:>{col2}.1f}% {after['dominant_pct']:>{col3}.1f}%"
    )
    print(
        f"  {'Artigos/dia (estimado)':<{col1}} "
        f"{before['total']:>{col2}} {after['total']:>{col3}}"
    )
    print("\n  [Proporções simuladas com volumes históricos típicos]\n")
    print("=" * 72)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Relatório de diversidade editorial Mapear-RN."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Janela histórica em dias (padrão: 7)",
    )
    args = parser.parse_args()
    main(days=args.days)
