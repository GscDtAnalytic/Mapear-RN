import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { fetchClusters, fetchRecentArticles, postSearch } from "../api/client";
import { useFilter } from "../context/FilterContext";
import type { ClusterData, SearchResult } from "../types";

const SENT_BADGE: Record<string, string> = {
  FAVORABLE: "badge-fav",
  WARNING: "badge-warn",
  ALERT: "badge-alert",
};
const SENT_PT: Record<string, string> = {
  FAVORABLE: "Positivo", WARNING: "Atenção", ALERT: "Crítico",
};

function ClusterGrid({ clusters }: { clusters: ClusterData[] }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
      {clusters.map((c) => (
        <div key={`${c.cluster_id}-${c.cluster_run_date}`}
          className="section-card hover:shadow-md transition-shadow">
          <div className="flex items-start justify-between mb-2">
            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-rn-bg text-rn-dark border border-rn-light">
              {c.cluster_label || `Cluster #${c.cluster_id}`}
            </span>
            <span className="text-xs text-gray-400">{new Date(c.cluster_run_date).toLocaleDateString("pt-BR")}</span>
          </div>
          <p className="text-sm text-gray-700 leading-snug line-clamp-3">{c.centroid_title ?? "—"}</p>
          <p className="text-xs text-rn-primary font-semibold mt-2">{c.article_count} artigos</p>
        </div>
      ))}
    </div>
  );
}

function sentimentBadge(score: number | null): [string, string] | null {
  if (score == null) return null;
  if (score >= 0.3) return ["badge-fav", "Positivo"];
  if (score <= -0.3) return ["badge-alert", "Crítico"];
  return ["badge-warn", "Neutro"];
}

function RecentArticle({ article }: { article: any }) {
  const [expanded, setExpanded] = useState(false);
  const badge = sentimentBadge(article.sentiment_overall);
  return (
    <div className="section-card hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between gap-2 mb-1">
        <p className="font-semibold text-sm text-rn-dark line-clamp-2 flex-1">{article.title}</p>
        {badge && (
          <span className={`${badge[0]} whitespace-nowrap`}>{badge[1]}</span>
        )}
      </div>
      <p className="text-xs text-gray-400 mb-2">
        {article.source_feed} · {new Date(article.published_at).toLocaleDateString("pt-BR")}
      </p>
      {article.narrative_summary && (
        <>
          <p className={`text-sm text-gray-600 leading-relaxed ${expanded ? "" : "line-clamp-3"}`}>
            {article.narrative_summary}
          </p>
          {article.narrative_summary.length > 200 && (
            <button onClick={() => setExpanded(!expanded)}
              className="text-xs text-rn-primary font-medium mt-1 hover:underline">
              {expanded ? "Ver menos" : "Ver mais"}
            </button>
          )}
        </>
      )}
    </div>
  );
}

function SearchBox() {
  const [query, setQuery] = useState("");
  const { mutate, data, isPending, isError, error } = useMutation<SearchResult, any, string>({
    mutationFn: (q) => postSearch(q),
  });

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    if (query.trim()) mutate(query.trim());
  };

  return (
    <div className="section-card">
      <p className="section-title text-base font-bold text-rn-dark">
        🔍 Busca semântica com IA
      </p>
      <p className="text-sm text-gray-500 mb-4">
        Faça uma pergunta sobre a política do RN. A IA busca nos summaries dos artigos e gera uma resposta com citações.
      </p>

      <form onSubmit={handleSearch} className="flex gap-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Ex: O que os candidatos dizem sobre saúde pública?"
          className="flex-1 px-4 py-2.5 rounded-lg border border-gray-300 text-sm focus:outline-none focus:border-rn-primary focus:ring-1 focus:ring-rn-primary"
        />
        <button type="submit" disabled={isPending || !query.trim()}
          className="px-5 py-2.5 bg-rn-primary text-white text-sm font-semibold rounded-lg
                     hover:bg-rn-med transition-colors disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap">
          {isPending ? "Buscando..." : "Buscar"}
        </button>
      </form>

      {isError && (
        <div className="mt-4 p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
          {(error as any)?.response?.data?.detail ?? "Erro na busca. Configure MAPEAR_LLM_API_KEY no servidor."}
        </div>
      )}

      {data && (
        <div className="mt-5">
          <div className="p-4 bg-rn-bg border border-rn-light rounded-xl mb-4">
            <p className="text-xs font-semibold text-rn-dark uppercase tracking-wide mb-2">Resposta da IA</p>
            <p className="text-sm text-gray-800 leading-relaxed">{data.answer}</p>
          </div>
          {data.sources.length > 0 && (
            <div>
              <p className="section-title">Fontes utilizadas ({data.sources.length})</p>
              <div className="space-y-2">
                {data.sources.map((s, i) => (
                  <div key={i} className="flex gap-3 p-3 bg-gray-50 rounded-lg">
                    <span className="text-xs font-bold text-rn-primary mt-0.5">[{i + 1}]</span>
                    <div>
                      <p className="text-sm font-medium text-gray-800">{s.title || "(sem título)"}</p>
                      <p className="text-xs text-gray-500 mt-0.5 line-clamp-2">{s.narrative_summary}</p>
                      <p className="text-xs text-gray-400 mt-0.5">
                        Similaridade: {((1 - s.distance) * 100).toFixed(0)}%
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function Narratives() {
  const { days } = useFilter();
  const { data: clusters = [] } = useQuery<ClusterData[]>({
    queryKey: ["clusters", days],
    queryFn: () => fetchClusters(days),
    staleTime: 10 * 60_000,
  });
  const { data: articles = [] } = useQuery<any[]>({
    queryKey: ["recent-articles", days],
    queryFn: () => fetchRecentArticles(Math.min(days, 14)),
    staleTime: 10 * 60_000,
  });

  return (
    <div>
      <div className="mb-5">
        <h1 className="page-title">Inteligência</h1>
        <p className="page-subtitle">Clusters narrativos e busca semântica sobre os summaries dos artigos.</p>
        <div className="mt-3 px-4 py-3 bg-amber-50 border border-amber-200 rounded-lg flex gap-2 items-start">
          <span className="text-amber-500 text-sm mt-0.5">⚠</span>
          <p className="text-xs text-amber-800 leading-relaxed">
            <strong>Apenas artigos de alto sinal de alerta</strong> recebem sumário narrativo (polarity ≤ −0,35 ou label ALERT).
            Por isso todos aparecem como "Crítico" — são literalmente os artigos mais preocupantes do período.
            Artigos positivos e neutros existem na base mas não passam pelo gate de custo da IA.
          </p>
        </div>
      </div>

      <div className="space-y-6">
        <SearchBox />

        {clusters.length > 0 && (
          <div>
            <h2 className="text-lg font-bold text-rn-dark mb-3">
              Clusters narrativos recentes
              <span className="ml-2 text-sm font-normal text-gray-400">{clusters.length} clusters</span>
            </h2>
            <ClusterGrid clusters={clusters} />
          </div>
        )}

        {articles.length > 0 && (
          <div>
            <h2 className="text-lg font-bold text-rn-dark mb-3">
              Artigos recentes com sumário
              <span className="ml-2 text-sm font-normal text-gray-400">{articles.length} artigos</span>
            </h2>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {articles.map((a: any) => <RecentArticle key={a.content_hash} article={a} />)}
            </div>
          </div>
        )}

        {clusters.length === 0 && articles.length === 0 && (
          <div className="section-card text-center py-10 text-gray-400 text-sm">
            Sem dados narrativos para o período. O pipeline de embeddings + clustering precisa ter rodado.
          </div>
        )}
      </div>
    </div>
  );
}
