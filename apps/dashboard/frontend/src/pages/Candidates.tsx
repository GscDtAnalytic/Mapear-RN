import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, Cell
} from "recharts";
import {
  fetchRanking, fetchSourceSplit, fetchGroupComparison,
  fetchEngagement, fetchMayors, fetchMayorEndorsements,
} from "../api/client";
import { useFilter } from "../context/FilterContext";
import { SentimentBar } from "../components/SentimentBar";
import type { Candidate, Mayor, MayorEndorsement } from "../types";

const TABS = [
  "🏆 Quem aparece mais",
  "📰 Imprensa vs Redes",
  "🏛️ Governo vs Oposição",
  "❤️ Engajamento",
  "🏙️ Prefeitos & Apoios",
];

const fmt = (n: number) => n.toLocaleString("pt-BR");
const RN = "#009B3A";
const COLORS_STACKED = { rss: "#3B82F6", social: "#8B5CF6" };
const SENT_COLORS: Record<string, string> = {
  FAVORABLE: "#2E8540", WARNING: "#E08C2B", ALERT: "#C8372D",
};
const SENT_PT: Record<string, string> = {
  FAVORABLE: "Positivo", WARNING: "Atenção", ALERT: "Crítico",
};

function RankingTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<Candidate[]>({
    queryKey: ["ranking", days],
    queryFn: () => fetchRanking(days),
    staleTime: 5 * 60_000,
  });

  const top10 = data.slice(0, 10).reverse();

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
      <div className="lg:col-span-2 section-card">
        <p className="section-title">Top candidatos a governador por menções</p>
        <ResponsiveContainer width="100%" height={380}>
          <BarChart data={top10} layout="vertical" margin={{ left: 20, right: 40 }}>
            <XAxis type="number" tick={{ fontSize: 11 }} />
            <YAxis dataKey="person_name" type="category" width={140} tick={{ fontSize: 12 }} />
            <Tooltip formatter={(v: number) => fmt(v)} />
            <Bar dataKey="mentions" name="Menções" radius={[0, 4, 4, 0]}>
              {top10.map((_, i) => <Cell key={i} fill={RN} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="section-card overflow-y-auto max-h-[460px]">
        <p className="section-title">Tom das menções (top 7)</p>
        <div className="space-y-4">
          {data.slice(0, 7).map((c) => (
            <div key={c.person_name} className="pb-3 border-b border-gray-100 last:border-0">
              <div className="flex justify-between items-baseline">
                <p className="font-semibold text-sm">{c.person_name}</p>
                <span className="text-xs text-sent-fav font-bold">
                  {c.fav + c.warn + c.alert > 0
                    ? `${Math.round((c.fav / (c.fav + c.warn + c.alert)) * 100)}% positivo`
                    : "—"}
                </span>
              </div>
              <SentimentBar fav={c.fav} warn={c.warn} alert={c.alert} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function SourceSplitTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<any[]>({
    queryKey: ["source-split", days],
    queryFn: () => fetchSourceSplit(days),
    staleTime: 5 * 60_000,
  });

  // pivot into person_name → { rss, social }
  const map: Record<string, { rss: number; social: number }> = {};
  for (const row of data) {
    if (!map[row.person_name]) map[row.person_name] = { rss: 0, social: 0 };
    if (row.source_type === "rss") map[row.person_name].rss += row.mentions;
    else map[row.person_name].social += row.mentions;
  }
  const chartData = Object.entries(map)
    .map(([name, v]) => ({ name, ...v, total: v.rss + v.social }))
    .sort((a, b) => b.total - a.total)
    .slice(0, 10)
    .reverse();

  return (
    <div className="section-card">
      <p className="section-title">Imprensa tradicional × Redes sociais</p>
      <ResponsiveContainer width="100%" height={400}>
        <BarChart data={chartData} layout="vertical" margin={{ left: 20, right: 20 }}>
          <XAxis type="number" tick={{ fontSize: 11 }} />
          <YAxis dataKey="name" type="category" width={140} tick={{ fontSize: 12 }} />
          <Tooltip formatter={(v: number) => fmt(v)} />
          <Legend />
          <Bar dataKey="rss" name="Imprensa" stackId="a" fill={COLORS_STACKED.rss} />
          <Bar dataKey="social" name="Redes sociais" stackId="a" fill={COLORS_STACKED.social} radius={[0, 4, 4, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function GroupComparisonTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<any[]>({
    queryKey: ["group-comparison", days],
    queryFn: () => fetchGroupComparison(days),
    staleTime: 5 * 60_000,
  });

  const chartData = data.flatMap((g: any) =>
    ["fav", "warn", "alert"].map((k) => ({
      grupo: g.grupo,
      label: SENT_PT[k === "fav" ? "FAVORABLE" : k === "warn" ? "WARNING" : "ALERT"],
      n: g[k],
    }))
  );

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
      {data.map((g: any) => (
        <div key={g.grupo} className="section-card">
          <p className="font-bold text-rn-dark text-lg">{g.grupo}</p>
          <p className="text-3xl font-extrabold text-rn-primary mt-1">{fmt(g.mentions)}</p>
          <p className="text-xs text-gray-400">menções · tom médio {g.avg_sentiment?.toFixed(2) ?? "—"}</p>
          <SentimentBar fav={g.fav} warn={g.warn} alert={g.alert} />
        </div>
      ))}
      <div className="lg:col-span-2 section-card">
        <p className="section-title">Distribuição de tom por grupo</p>
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={data} margin={{ left: 10, right: 20 }}>
            <XAxis dataKey="grupo" tick={{ fontSize: 13 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip formatter={(v: number) => fmt(v)} />
            <Legend />
            <Bar dataKey="fav"   name="Positivo" stackId="a" fill={SENT_COLORS.FAVORABLE} />
            <Bar dataKey="warn"  name="Atenção"  stackId="a" fill={SENT_COLORS.WARNING} />
            <Bar dataKey="alert" name="Crítico"  stackId="a" fill={SENT_COLORS.ALERT} radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function EngagementTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<any[]>({
    queryKey: ["engagement", days],
    queryFn: () => fetchEngagement(days),
    staleTime: 5 * 60_000,
  });

  if (!data.length) return (
    <div className="section-card text-center py-10 text-gray-400 text-sm">
      Ainda não há dados de interação. Tente ampliar o período para ≥ 30 dias.
    </div>
  );

  // heatmap: person × platform
  const persons = [...new Set(data.map((r: any) => r.person_name))];
  const platforms = [...new Set(data.map((r: any) => r.platform))];
  const lookup: Record<string, Record<string, number>> = {};
  for (const r of data) {
    if (!lookup[r.person_name]) lookup[r.person_name] = {};
    lookup[r.person_name][r.platform] = r.engagement;
  }
  const maxVal = Math.max(...data.map((r: any) => r.engagement));

  return (
    <div className="section-card overflow-x-auto">
      <p className="section-title">Interações nas redes (curtidas + comentários + compartilhamentos)</p>
      <table className="w-full text-sm mt-2">
        <thead>
          <tr>
            <th className="text-left pb-2 pr-4 text-gray-500 font-medium">Candidato</th>
            {platforms.map((p) => <th key={p} className="pb-2 px-3 text-gray-500 font-medium">{p}</th>)}
          </tr>
        </thead>
        <tbody>
          {persons.map((person) => (
            <tr key={person} className="border-t border-gray-100">
              <td className="py-2 pr-4 font-medium text-rn-dark text-xs">{person}</td>
              {platforms.map((p) => {
                const v = lookup[person]?.[p] ?? 0;
                const intensity = maxVal > 0 ? v / maxVal : 0;
                const bg = intensity === 0
                  ? "bg-gray-50"
                  : intensity < 0.3 ? "bg-green-100" : intensity < 0.7 ? "bg-green-300" : "bg-rn-primary";
                const text = intensity > 0.6 ? "text-white" : "text-gray-800";
                return (
                  <td key={p} className={`py-2 px-3 text-center rounded text-xs font-bold ${bg} ${text}`}>
                    {v > 0 ? fmt(v) : "—"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// "Indefinido" / vazio = apoio ainda não identificado (sentinela do seed/LLM).
function isUndefinedSupport(raw: string | null | undefined): boolean {
  const v = (raw ?? "").trim();
  return !v || v.toLowerCase() === "indefinido";
}

const CONF_PT: Record<string, string> = { alta: "alta", media: "média", baixa: "baixa" };
const CONF_CLS: Record<string, string> = {
  alta: "text-sent-fav", media: "text-sent-warn", baixa: "text-gray-400",
};

function MayorCard({ mayor, endorsement }: { mayor: Mayor; endorsement?: MayorEndorsement }) {
  const verdict = endorsement?.endorsed_candidate ?? null;
  const hasVerdict = !isUndefinedSupport(verdict);
  const isLLM = endorsement?.endorsement_source === "llm";
  const conf = endorsement?.llm_confidence ?? null;
  // Quando a curadoria sobrescreveu, mostramos o que a IA havia concluído.
  const llmDiffers =
    endorsement?.endorsement_source === "manual" &&
    !isUndefinedSupport(endorsement?.llm_candidate) &&
    endorsement?.llm_candidate !== verdict;

  return (
    <div className="section-card">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-bold text-rn-dark truncate">{mayor.person_name}</p>
          <p className="text-xs text-gray-400">
            {mayor.person_city} · {mayor.person_party ?? "sem partido"} · {fmt(mayor.mentions)} menções
          </p>
        </div>
        {hasVerdict ? (
          <span className="badge-fav whitespace-nowrap">🤝 Apoia {verdict}</span>
        ) : (
          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-gray-100 text-gray-500 whitespace-nowrap">
            Apoio não identificado
          </span>
        )}
      </div>

      {mayor.mentions > 0
        ? <SentimentBar fav={mayor.fav} warn={mayor.warn} alert={mayor.alert} />
        : <p className="text-xs text-gray-400 mt-2">Sem menções no período.</p>}

      <div className="mt-3 border-t border-gray-100 pt-3">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
          Investigação de apoio
        </p>
        {!endorsement ? (
          <p className="text-xs text-gray-400">Investigação ainda não executada.</p>
        ) : (
          <div className="space-y-1.5">
            <p className="text-xs">
              {isLLM ? (
                <>
                  <span className="font-semibold text-rn-dark">🔍 Detectado pela IA</span>
                  {conf && (
                    <span className={CONF_CLS[conf] ?? "text-gray-400"}>
                      {" "}· confiança {CONF_PT[conf] ?? conf}
                    </span>
                  )}
                </>
              ) : (
                <span className="font-semibold text-rn-dark">✍️ Definido por curadoria manual</span>
              )}
            </p>
            {llmDiffers && (
              <p className="text-xs text-gray-400">
                A IA havia detectado: {endorsement!.llm_candidate}
              </p>
            )}
            {endorsement.llm_rationale && (
              <p className="text-sm text-gray-600 leading-relaxed">{endorsement.llm_rationale}</p>
            )}
            {endorsement.article_count != null && endorsement.article_count > 0 && (
              <p className="text-xs text-gray-400">
                {fmt(endorsement.article_count)} artigos analisados
                {endorsement.endorsement_model ? ` · ${endorsement.endorsement_model}` : ""}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function MayorsTab() {
  const { days } = useFilter();
  const { data: mayors = [] } = useQuery<Mayor[]>({
    queryKey: ["mayors", days],
    queryFn: () => fetchMayors(days),
    staleTime: 5 * 60_000,
  });
  const { data: endorsements = [] } = useQuery<MayorEndorsement[]>({
    queryKey: ["mayor-endorsements"],
    queryFn: () => fetchMayorEndorsements(days),
    staleTime: 5 * 60_000,
  });

  const byCity: Record<string, MayorEndorsement> = {};
  for (const e of endorsements) byCity[e.city] = e;

  const chartData = [...mayors].reverse();

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <div className="lg:col-span-2 section-card">
          <p className="section-title">Prefeitos das 5 maiores cidades do RN — menções</p>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={chartData} layout="vertical" margin={{ left: 20, right: 40 }}>
              <XAxis type="number" tick={{ fontSize: 11 }} />
              <YAxis dataKey="person_city" type="category" width={120} tick={{ fontSize: 12 }} />
              <Tooltip formatter={(v: number) => fmt(v)} />
              <Bar dataKey="mentions" name="Menções" radius={[0, 4, 4, 0]}>
                {chartData.map((_, i) => <Cell key={i} fill={RN} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="section-card text-sm text-gray-600 leading-relaxed">
          <p className="section-title">Como ler esta aba</p>
          <p className="mb-2">
            O <strong>apoio</strong> é investigado por IA (Claude Sonnet): o modelo lê
            as notícias que ligam o prefeito a um candidato e julga se há aproximação
            política — com nível de confiança e justificativa.
          </p>
          <p>
            A <strong>curadoria manual</strong> sobrescreve o veredito da IA quando
            você preenche o apoio no seed; nesse caso o card mostra o que a IA havia
            concluído, para comparação.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        {mayors.map((m) => (
          <MayorCard key={m.person_city} mayor={m} endorsement={byCity[m.person_city]} />
        ))}
      </div>
    </div>
  );
}

export function Candidates() {
  const [activeTab, setActiveTab] = useState(0);

  return (
    <div>
      <div className="mb-5">
        <h1 className="page-title">A Corrida</h1>
        <p className="page-subtitle">Quem domina a narrativa política no Rio Grande do Norte.</p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-200 mb-5 overflow-x-auto">
        {TABS.map((t, i) => (
          <button
            key={i}
            onClick={() => setActiveTab(i)}
            className={`px-4 py-2.5 text-sm whitespace-nowrap transition-colors ${
              activeTab === i ? "tab-active" : "tab-inactive"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {activeTab === 0 && <RankingTab />}
      {activeTab === 1 && <SourceSplitTab />}
      {activeTab === 2 && <GroupComparisonTab />}
      {activeTab === 3 && <EngagementTab />}
      {activeTab === 4 && <MayorsTab />}
    </div>
  );
}
