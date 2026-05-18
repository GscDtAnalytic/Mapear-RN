"""Seed initial RSS feed sources into the database.

Usage:
    poetry run python scripts/seed_feeds.py

Adds the curated list of RN-focused and national RSS feeds
to the feed_sources table in PostgreSQL.
"""

from sqlalchemy import create_engine, text

from mapear_infra.config import get_settings

# Feeds priorizados para cobertura do RN
FEEDS = [
    # --- Portais locais do RN (prioridade alta) ---
    {
        "name": "Tribuna do Norte",
        "url": "https://tribunadonorte.com.br/feed/",
        "category": "rn_local",
        "priority": 10,
        "is_rn_focused": True,
    },
    {
        "name": "Novo Notícias",
        "url": "https://www.novonoticias.com.br/feed/",
        "category": "rn_local",
        "priority": 9,
        "is_rn_focused": True,
    },
    {
        "name": "Agora RN",
        "url": "https://agorarn.com.br/feed/",
        "category": "rn_local",
        "priority": 9,
        "is_rn_focused": True,
    },
    {
        "name": "Blog do BG",
        "url": "https://blogdobg.com.br/feed/",
        "category": "rn_local",
        "priority": 8,
        "is_rn_focused": True,
    },
    {
        "name": "Saiba Mais",
        "url": "https://saibamais.jor.br/feed/",
        "category": "rn_local",
        "priority": 8,
        "is_rn_focused": True,
    },
    # --- Portais regionais do RN (cobertura interior/cidades-alvo) ---
    {
        "name": "O Potengi",
        "url": "https://opotengi.com.br/feed/",
        "category": "rn_regional",
        "priority": 8,
        "is_rn_focused": True,
    },
    {
        "name": "Ponta Negra News",
        "url": "https://pontanegranews.com.br/feed/",
        "category": "rn_local",
        "priority": 8,
        "is_rn_focused": True,
    },
    {
        "name": "O Poti",
        "url": "https://opoti.com.br/feed/",
        "category": "rn_local",
        "priority": 8,
        "is_rn_focused": True,
    },
    {
        "name": "Por Dentro do RN",
        "url": "https://pordentrodorn.com.br/feed/",
        "category": "rn_regional",
        "priority": 7,
        "is_rn_focused": True,
    },
    {
        "name": "Café com Notícias RN",
        "url": "https://cafecomnoticiasrn.com.br/feed/",
        "category": "rn_regional",
        "priority": 7,
        "is_rn_focused": True,
    },
    {
        "name": "Portal do RN",
        "url": "https://portaldorn.com/feed/",
        "category": "rn_regional",
        "priority": 7,
        "is_rn_focused": True,
    },
    {
        "name": "Blog do Seridó",
        "url": "https://blogdoserido.com.br/feed/",
        "category": "rn_regional",
        "priority": 7,
        "is_rn_focused": True,
    },
    {
        "name": "Blog do Robson Pires",
        "url": "https://robsonpiresxerife.com/feed/",
        "category": "rn_regional",
        "priority": 6,
        "is_rn_focused": True,
    },
    # --- Novos portais RN (expansão de diversidade editorial 2026-04) ---
    {
        "name": "Novo Jornal RN",
        "url": "https://novojornal.jor.br/feed/",
        "category": "rn_local",
        "priority": 9,
        "is_rn_focused": True,
    },
    {
        "name": "Portal No Ar",
        "url": "https://portalnoar.com.br/feed/",
        "category": "rn_local",
        "priority": 8,
        "is_rn_focused": True,
    },
    {
        "name": "Mossoró Hoje",
        "url": "https://mossoro.news/feed/",
        "category": "rn_regional",
        "priority": 8,
        "is_rn_focused": True,
    },
    {
        "name": "Foco RN",
        "url": "https://focorn.com.br/feed/",
        "category": "rn_local",
        "priority": 7,
        "is_rn_focused": True,
    },
    {
        "name": "Seridó Online",
        "url": "https://seridoonline.com.br/feed/",
        "category": "rn_regional",
        "priority": 7,
        "is_rn_focused": True,
    },
    {
        "name": "RN em Pauta",
        "url": "https://rnempauta.com.br/feed/",
        "category": "rn_local",
        "priority": 7,
        "is_rn_focused": True,
    },
    {
        "name": "Pontal FM",
        "url": "https://pontalfm.com.br/feed/",
        "category": "rn_regional",
        "priority": 6,
        "is_rn_focused": True,
    },
    # --- Portais nacionais com editoria Nordeste/RN ---
    {
        "name": "G1 RN",
        "url": "https://g1.globo.com/rss/g1/rn/rio-grande-do-norte/",
        "category": "nacional_rn",
        "priority": 9,
        "is_rn_focused": True,
    },
    # UOL Notícias removido: retorna 403 para todos os artigos (bloqueio
    # anti-scraping). Não é fonte RN-específica e polui o frontier com
    # URLs que falham consistentemente.
    # {
    #     "name": "UOL Notícias",
    #     "url": "https://rss.uol.com.br/feed/noticias.xml",
    #     "category": "nacional",
    #     "priority": 5,
    #     "is_rn_focused": False,
    # },
    {
        "name": "Folha de S.Paulo",
        "url": "https://feeds.folha.uol.com.br/poder/rss091.xml",
        "category": "nacional_politica",
        "priority": 6,
        "is_rn_focused": False,
    },
    {
        "name": "Estadão Política",
        "url": "https://www.estadao.com.br/arc/outboundfeeds/rss/section/politica?outputType=xml",
        "category": "nacional_politica",
        "priority": 6,
        "is_rn_focused": False,
    },
    {
        "name": "Poder360",
        "url": "https://www.poder360.com.br/feed/",
        "category": "nacional_politica",
        "priority": 7,
        "is_rn_focused": False,
    },
    {
        "name": "Congresso em Foco",
        "url": "https://congressoemfoco.uol.com.br/feed/",
        "category": "nacional_politica",
        "priority": 6,
        "is_rn_focused": False,
    },
]


def seed_feeds() -> None:
    """Insert feed sources into the database."""
    settings = get_settings()
    engine = create_engine(settings.postgres.dsn)

    with engine.begin() as conn:
        for feed in FEEDS:
            conn.execute(
                text(
                    """
                    INSERT INTO feed_sources (name, url, category, priority, is_rn_focused)
                    VALUES (:name, :url, :category, :priority, :is_rn_focused)
                    ON CONFLICT (name) DO UPDATE SET
                        url = :url,
                        category = :category,
                        priority = :priority,
                        is_rn_focused = :is_rn_focused,
                        updated_at = NOW()
                """
                ),
                feed,
            )

    print(f"Seeded {len(FEEDS)} feeds into feed_sources table.")


if __name__ == "__main__":
    seed_feeds()
