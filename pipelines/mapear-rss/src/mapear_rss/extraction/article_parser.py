"""Article content extraction using trafilatura.

Extracts clean text, title, author, and date from HTML pages
using bare_extraction for structured metadata access.
"""

import contextlib
import json
from collections.abc import Callable
from datetime import datetime

import trafilatura
from bs4 import BeautifulSoup
from loguru import logger
from lxml import html as lxml_html
from pydantic import HttpUrl
from readability import Document

from mapear_domain.models.base import RawArticle
from mapear_rss.extraction.content_hasher import hash_content

_JSONLD_ARTICLE_TYPES = {
    "NewsArticle",
    "Article",
    "ReportageNewsArticle",
    "BlogPosting",
    "OpinionNewsArticle",
}

_MIN_EXTRACTED_CHARS = 100

# Domain-specific CSS selectors. Used when trafilatura, JSON-LD e
# readability falham num portal cuja marcação é "exótica" o suficiente
# para passar por todas as estratégias genéricas. Mapping de
# ``netloc`` (sem ``www.``) → seletor CSS do container do corpo do
# artigo. Mantenha conservador — só adicione domínios depois de
# verificar a marcação real do site.
_DOMAIN_SELECTORS: dict[str, str] = {
    # agorarn: NewsArticle JSON-LD present but no articleBody field;
    # trafilatura now extracts it but the CSS selector is kept as backup.
    "agorarn.com.br": "div.contentnotice",
    # novonoticias: WordPress-derived theme; trafilatura works but
    # internal-single__text is the canonical article body container.
    "novonoticias.com.br": "div.internal-single__text",
    # blogdobg: standard WordPress entry-content; trafilatura handles it
    # well but the explicit selector is more robust against theme changes.
    "blogdobg.com.br": "div.entry-content",
    # tribunadonorte: Tailwind-based theme with no stable class names;
    # the semantic <article> tag wraps the full post reliably.
    "tribunadonorte.com.br": "article",
}


def _iter_jsonld_nodes(data):
    """Walk a decoded JSON-LD payload yielding every dict node.

    JSON-LD bodies can be a single object, a list, or nested under
    ``@graph``. We need to see all of them to find the article node.
    """
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_nodes(item)
        return
    if isinstance(data, dict):
        yield data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_jsonld_nodes(item)


class ArticleParser:
    """Parses HTML pages into structured RawArticle objects."""

    def __init__(
        self,
        on_recovery: Callable[[str, str], None] | None = None,
    ) -> None:
        # Optional callback invoked as ``on_recovery(domain, strategy)``
        # when a non-primary strategy wins. Used by the scraper to feed
        # ``DiagnosticCollector.note_parser_recovery``. Left ``None`` by
        # default so this module stays usable in isolation / tests.
        self._on_recovery = on_recovery

    def parse(
        self,
        html: str,
        url: str,
        source_feed: str,
        feed_published_at: datetime | None = None,
    ) -> RawArticle | None:
        """Extract article content from HTML.

        Uses trafilatura.bare_extraction which returns a dict with
        title, text, author, date, and other metadata in one call.

        Args:
            html: Raw HTML string.
            url: The article URL (for metadata).
            source_feed: The feed that discovered this URL.
            feed_published_at: Date from the RSS feed entry (fallback).

        Returns:
            RawArticle if extraction succeeds, None otherwise.
        """
        if not html or not html.strip():
            logger.debug("Empty HTML for {url}", url=url)
            return None

        meta = None
        strategy_used = "trafilatura_precision"
        try:
            meta = trafilatura.bare_extraction(
                html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                url=url,
            )
        except Exception as e:
            logger.warning(
                "trafilatura crashed for {url}: {error}",
                url=url,
                error=str(e),
            )

        _meta_text = meta.get("text", "") if meta else ""
        _text_ok = bool(_meta_text) and len(_meta_text.strip()) >= _MIN_EXTRACTED_CHARS

        # Fallback 1: JSON-LD <script type="application/ld+json"> with
        # NewsArticle.articleBody. Runs before the trafilatura recall
        # pass because when it's available it gives us the canonical
        # editor-blessed body.
        if not _text_ok:
            jsonld_meta = self._extract_jsonld(html)
            if jsonld_meta:
                logger.debug("jsonld fallback succeeded for {url}", url=url)
                meta = jsonld_meta
                _meta_text = jsonld_meta["text"]
                _text_ok = True
                strategy_used = "jsonld"

        # Fallback 1b: domain-specific selector. Para portais que ficaram
        # famosos por driblar todas as estratégias genéricas (agorarn
        # entrega NewsArticle JSON-LD SEM articleBody e o trafilatura
        # extrai 0 chars), usamos um seletor CSS específico do domínio.
        if not _text_ok:
            domain_meta = self._extract_with_domain_selector(html, url)
            if domain_meta:
                logger.debug("domain selector succeeded for {url}", url=url)
                meta = domain_meta
                _meta_text = domain_meta["text"]
                _text_ok = True
                strategy_used = "domain_selector"

        # Fallback 2: retry without favor_precision for heavy-template
        # Brazilian portals (blogdobg, tribunadonorte, agorarn, saibamais).
        if not _text_ok:
            try:
                meta_fallback = trafilatura.bare_extraction(
                    html,
                    include_comments=False,
                    include_tables=False,
                    favor_precision=False,
                    url=url,
                )
                if (
                    meta_fallback
                    and meta_fallback.get("text")
                    and len(meta_fallback.get("text", "").strip())
                    >= _MIN_EXTRACTED_CHARS
                ):
                    logger.debug(
                        "bare_extraction fallback (favor_precision=False) "
                        "succeeded for {url}",
                        url=url,
                    )
                    meta = meta_fallback
                    _meta_text = meta_fallback.get("text", "")
                    _text_ok = True
                    strategy_used = "trafilatura_recall"
            except Exception as e:
                logger.warning(
                    "trafilatura fallback also crashed for {url}: {error}",
                    url=url,
                    error=str(e),
                )

        # Fallback 3: readability-lxml + BeautifulSoup — fully independent.
        if not _text_ok:
            readability_meta = self._extract_with_readability(html, url)
            if readability_meta:
                logger.debug("readability fallback succeeded for {url}", url=url)
                meta = readability_meta
                _meta_text = readability_meta["text"]
                _text_ok = True
                strategy_used = "readability"

        if not meta:
            logger.debug("bare_extraction returned None for {url}", url=url)
            return None

        text = meta.get("text", "")
        if not text or len(text.strip()) < 100:
            logger.debug(
                "Insufficient content extracted from {url}",
                url=url,
            )
            return None

        title = meta.get("title")
        if not title:
            title = self._extract_title_fallback(html)
        if not title:
            logger.debug("No title found for {url}", url=url)
            return None

        author = meta.get("author")

        # Fallback chain: trafilatura date → feed published_at → now
        published_at = self._parse_date(meta.get("date"))
        if published_at is None and feed_published_at is not None:
            published_at = feed_published_at
            logger.debug(
                "Using feed published_at for {url}: {date}",
                url=url,
                date=feed_published_at.isoformat(),
            )

        content_hash_val = hash_content(title, text)

        try:
            article = RawArticle(
                url=HttpUrl(url),
                source_feed=source_feed,
                title=title,
                content=text,
                author=author,
                published_at=published_at,
                content_hash=content_hash_val,
            )
        except Exception as e:
            logger.warning(
                "Failed to create RawArticle for {url}: {error}",
                url=url,
                error=str(e),
            )
            return None

        if self._on_recovery is not None and strategy_used != "trafilatura_precision":
            try:
                from urllib.parse import urlparse

                self._on_recovery(urlparse(url).netloc, strategy_used)
            except Exception:
                logger.debug("parser recovery callback raised; ignoring")

        return article

    @staticmethod
    def _extract_jsonld(html_str: str) -> dict | None:
        """Extract article body from JSON-LD structured data.

        Looks for ``<script type="application/ld+json">`` blocks whose
        ``@type`` is a NewsArticle-like schema.org type and that expose
        an ``articleBody`` field. Returns a dict shaped like
        trafilatura's bare_extraction output, or ``None`` if nothing
        usable is found.
        """
        try:
            tree = lxml_html.fromstring(html_str)
        except Exception:
            return None

        scripts = tree.xpath('//script[@type="application/ld+json"]/text()')
        for script_text in scripts:
            if not script_text or not script_text.strip():
                continue
            try:
                data = json.loads(script_text)
            except ValueError:
                continue

            for node in _iter_jsonld_nodes(data):
                if not isinstance(node, dict):
                    continue
                types = node.get("@type")
                if isinstance(types, str):
                    types_set = {types}
                elif isinstance(types, list):
                    types_set = {t for t in types if isinstance(t, str)}
                else:
                    continue
                if not types_set & _JSONLD_ARTICLE_TYPES:
                    continue

                body = node.get("articleBody")
                if not isinstance(body, str):
                    continue
                if len(body.strip()) < _MIN_EXTRACTED_CHARS:
                    continue

                headline = node.get("headline") or node.get("name")
                if isinstance(headline, list):
                    headline = headline[0] if headline else None
                author_field = node.get("author")
                if isinstance(author_field, dict):
                    author = author_field.get("name")
                elif isinstance(author_field, list) and author_field:
                    first = author_field[0]
                    author = first.get("name") if isinstance(first, dict) else None
                elif isinstance(author_field, str):
                    author = author_field
                else:
                    author = None

                date = (
                    node.get("datePublished")
                    or node.get("dateCreated")
                    or node.get("dateModified")
                )

                return {
                    "text": body.strip(),
                    "title": headline if isinstance(headline, str) else None,
                    "author": author,
                    "date": date if isinstance(date, str) else None,
                }
        return None

    @staticmethod
    def _extract_with_domain_selector(html_str: str, url: str) -> dict | None:
        """Extract body text using a domain-specific CSS selector.

        Looks up the article URL's domain (sem ``www.``) em
        ``_DOMAIN_SELECTORS`` e, se existir um seletor, usa BeautifulSoup
        para isolar o container do corpo. Devolve um dict no formato do
        ``trafilatura.bare_extraction`` ou ``None`` se o domínio não
        estiver mapeado, o seletor não casar, ou o texto extraído for
        muito curto.
        """
        try:
            from urllib.parse import urlparse

            netloc = urlparse(url).netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]

            selector = _DOMAIN_SELECTORS.get(netloc)
            if not selector:
                return None

            soup = BeautifulSoup(html_str, "lxml")
            container = soup.select_one(selector)
            if container is None:
                return None

            # Drop boilerplate inside the container before extracting text.
            for tag in container(["script", "style", "nav", "footer", "aside"]):
                tag.decompose()

            text = container.get_text(separator="\n", strip=True)
            text = "\n".join(line for line in text.splitlines() if line.strip())

            if not text or len(text.strip()) < _MIN_EXTRACTED_CHARS:
                return None

            # Reuse generic title/date helpers from the surrounding HTML —
            # the selector only delimita o corpo, não os metadados.
            title = ArticleParser._extract_title_fallback(html_str)

            return {"text": text, "title": title, "author": None, "date": None}
        except Exception as e:
            logger.warning(
                "domain selector fallback crashed for {url}: {error}",
                url=url,
                error=str(e),
            )
            return None

    @staticmethod
    def _extract_with_readability(html_str: str, url: str) -> dict | None:
        """Extract main content via readability-lxml + BeautifulSoup.

        Independent parser path used when trafilatura fails. Returns a
        dict shaped like trafilatura's bare_extraction output so the
        caller can consume it uniformly. Returns None on any failure
        or if extracted text is shorter than 100 chars.
        """
        try:
            doc = Document(html_str)
            summary_html = doc.summary(html_partial=True)
            title = doc.short_title() or None

            soup = BeautifulSoup(summary_html, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = "\n".join(line for line in text.splitlines() if line.strip())

            if not text or len(text.strip()) < 100:
                return None

            return {"text": text, "title": title, "author": None, "date": None}
        except Exception as e:
            logger.warning(
                "readability fallback crashed for {url}: {error}",
                url=url,
                error=str(e),
            )
            return None

    @staticmethod
    def _extract_title_fallback(html_str: str) -> str | None:
        """Extract title from HTML via og:title, <title>, or <h1>."""
        try:
            tree = lxml_html.fromstring(html_str)
            # og:title
            og = tree.xpath('//meta[@property="og:title"]/@content')
            if og and og[0].strip():
                return og[0].strip()
            # <h1>
            h1 = tree.xpath("//h1//text()")
            h1_text = " ".join(t.strip() for t in h1 if t.strip())
            if h1_text:
                return h1_text
            # <title>
            t = tree.xpath("//title/text()")
            if t and t[0].strip():
                return t[0].strip()
        except Exception as e:
            logger.debug("Title fallback extraction failed: {error}", error=str(e))
        return None

    @staticmethod
    def _parse_date(date_str: str | None) -> datetime | None:
        """Parse a date string from trafilatura into datetime."""
        if not date_str:
            return None

        # trafilatura retorna datas em formato ISO (YYYY-MM-DD)
        with contextlib.suppress(ValueError, TypeError):
            return datetime.fromisoformat(date_str)

        return None
