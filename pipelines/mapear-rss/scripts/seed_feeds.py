"""Seed initial RSS feed sources into the database.

Usage:
    poetry run python scripts/seed_feeds.py

Adds the curated list of RN-focused and national RSS feeds
to the feed_sources table in PostgreSQL.
"""

from loguru import logger
from sqlalchemy import create_engine, text
from tenacity import retry, stop_after_attempt, wait_exponential

from mapear_rss.config import get_rss_settings

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
        # /feed/ retornou HTTP 405 em 2 runs consecutivos (2026-04-24).
        # /rss/ é o path canônico e retorna 200 consistentemente.
        "url": "https://congressoemfoco.uol.com.br/rss/",
        "category": "nacional_politica",
        "priority": 6,
        "is_rn_focused": False,
    },
]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def seed_feeds() -> None:
    """Insert feed sources into the database."""
    settings = get_rss_settings()
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

    logger.info("Seeded {count} feeds into feed_sources table", count=len(FEEDS))


if __name__ == "__main__":
    try:
        seed_feeds()
    except Exception:
        logger.exception("Failed to seed feeds after retries")
        raise
