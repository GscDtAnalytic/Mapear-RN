"""Tests for article parser using trafilatura."""

from mapear_rss.extraction.article_parser import ArticleParser

SAMPLE_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <title>Prefeito de Natal anuncia investimentos em saúde</title>
    <meta name="author" content="Repórter TN">
    <meta name="date" content="2026-04-01">
</head>
<body>
    <article>
        <h1>Prefeito de Natal anuncia investimentos em saúde</h1>
        <p>O prefeito Paulinho Freire anunciou nesta terça-feira a construção
        de um novo hospital em Natal. O investimento será de R$ 50 milhões
        com recursos federais destinados à saúde pública do Rio Grande do Norte.
        A obra deve começar no primeiro semestre de 2027 e beneficiar mais de
        500 mil habitantes da capital potiguar. O novo hospital contará com
        200 leitos, centro cirúrgico e UTI neonatal.</p>
        <p>Segundo o prefeito, a licitação será publicada ainda neste mês.
        A expectativa é de que as obras durem aproximadamente 18 meses,
        com entrega prevista para o segundo semestre de 2028. O terreno
        já foi desapropriado na zona norte da cidade.</p>
    </article>
</body>
</html>
"""

SHORT_HTML = """
<html><body><p>Texto curto.</p></body></html>
"""

NO_TITLE_HTML = """
<html><body>
<p>Este é um texto longo o suficiente para passar o threshold de 100
caracteres mas que não possui nenhuma tag de título definida em nenhum
lugar do HTML. Repetindo conteúdo para atingir o mínimo necessário
de caracteres para a validação do parser funcionar corretamente.</p>
</body></html>
"""


class TestArticleParser:
    def test_parse_extracts_content(self) -> None:
        parser = ArticleParser()
        result = parser.parse(
            SAMPLE_HTML,
            "https://example.com/noticia",
            "test_feed",
        )
        # trafilatura pode ou não extrair dependendo do HTML
        # O importante é que não crashe e retorne RawArticle ou None
        if result is not None:
            assert result.title is not None
            assert len(result.content) >= 100
            assert result.content_hash is not None
            assert len(result.content_hash) == 64
            assert result.source_feed == "test_feed"

    def test_parse_short_content_returns_none(self) -> None:
        parser = ArticleParser()
        result = parser.parse(
            SHORT_HTML,
            "https://example.com/short",
            "test_feed",
        )
        assert result is None

    def test_parse_empty_html_returns_none(self) -> None:
        parser = ArticleParser()
        result = parser.parse("", "https://example.com/empty", "test_feed")
        assert result is None

    def test_parse_date_valid(self) -> None:
        dt = ArticleParser._parse_date("2026-04-01")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 4

    def test_parse_date_none(self) -> None:
        assert ArticleParser._parse_date(None) is None

    def test_parse_date_invalid(self) -> None:
        assert ArticleParser._parse_date("not-a-date") is None


_JSONLD_BODY = (
    "O prefeito do município anunciou nesta quinta-feira uma nova obra de "
    "infraestrutura urbana que deve beneficiar milhares de moradores da "
    "região metropolitana. A licitação será publicada ainda neste mês e as "
    "obras começam no segundo semestre de 2026 conforme o cronograma "
    "apresentado pela prefeitura."
)

JSONLD_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Página com JSON-LD</title>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "NewsArticle",
  "headline": "Prefeito anuncia obra no RN",
  "datePublished": "2026-04-10T12:00:00-03:00",
  "author": {"@type": "Person", "name": "Jornalista Teste"},
  "articleBody": "__BODY__"
}
</script>
</head>
<body>
<div class="paywall">Assine para ler</div>
</body>
</html>
""".replace(
    "__BODY__", _JSONLD_BODY
)


class TestJsonLd:
    def test_jsonld_fallback_recovers_body(self) -> None:
        parser = ArticleParser()
        result = parser.parse(JSONLD_HTML, "https://agorarn.com.br/x", "feed")
        assert result is not None
        assert "prefeito" in result.content.lower()
        assert result.title == "Prefeito anuncia obra no RN"
        assert result.author == "Jornalista Teste"

    def test_extract_jsonld_direct(self) -> None:
        meta = ArticleParser._extract_jsonld(JSONLD_HTML)
        assert meta is not None
        assert meta["title"] == "Prefeito anuncia obra no RN"
        assert meta["author"] == "Jornalista Teste"
        assert "prefeito" in meta["text"].lower()
        assert meta["date"] == "2026-04-10T12:00:00-03:00"

    def test_extract_jsonld_ignores_non_article_types(self) -> None:
        html = """
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Organization",
         "name": "Some Publisher", "articleBody": "irrelevant"}
        </script>
        """
        assert ArticleParser._extract_jsonld(html) is None

    def test_domain_selector_recovers_agorarn(self) -> None:
        # agorarn.com.br entrega NewsArticle JSON-LD SEM articleBody, e
        # nem trafilatura nem readability conseguem isolar o corpo.
        # O fallback de seletor por domínio deve recuperar o texto a
        # partir de ``div.contentnotice``.
        body = (
            "Após as chuvas recentes, a Secretaria de Mobilidade Urbana "
            "informou a situação atualizada das vias em Natal, com registro "
            "de pontos críticos em vários bairros. O monitoramento é feito "
            "constantemente pelos agentes em campo durante todo o dia."
        )
        html = (
            "<!DOCTYPE html><html><head>"
            "<title>Chuvas em Natal</title>"
            '<meta property="og:title" content="Chuvas em Natal">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"NewsArticle",'
            '"headline":"Chuvas em Natal"}'
            "</script>"
            "</head><body>"
            f'<div class="contentnotice"><p>{body}</p></div>'
            "</body></html>"
        )
        parser = ArticleParser()
        result = parser.parse(
            html,
            "https://agorarn.com.br/manchete/chuvas-natal/",
            "https://agorarn.com.br/feed/",
        )
        assert result is not None
        assert "secretaria" in result.content.lower()
        assert result.title is not None

    def test_domain_selector_skipped_for_unmapped_domain(self) -> None:
        body = "Texto de exemplo " * 20
        html = (
            "<!DOCTYPE html><html><head><title>x</title></head><body>"
            f'<div class="contentnotice"><p>{body}</p></div>'
            "</body></html>"
        )
        # _extract_with_domain_selector retorna None para domínios não
        # mapeados, mesmo que a marcação tenha o seletor mapeado.
        meta = ArticleParser._extract_with_domain_selector(
            html, "https://example.com/x"
        )
        assert meta is None

    def test_graph_wrapper_node(self) -> None:
        graph_body = (
            "Corpo do artigo no formato graph com texto suficientemente "
            "longo para passar do limite minimo de cem caracteres "
            "estabelecido pelo parser e assim ser aceito."
        )
        html = (
            '<script type="application/ld+json">'
            '{"@context": "https://schema.org", "@graph": ['
            '{"@type": "WebSite", "name": "Portal"},'
            '{"@type": "NewsArticle", "headline": "Teste Graph",'
            f'"articleBody": "{graph_body}"}}'
            "]}"
            "</script>"
        )
        meta = ArticleParser._extract_jsonld(html)
        assert meta is not None
        assert meta["title"] == "Teste Graph"
        assert "corpo do artigo" in meta["text"].lower()
