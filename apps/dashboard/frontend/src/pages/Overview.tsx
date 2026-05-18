import { useQuery } from "@tanstack/react-query";
import { MapContainer, TileLayer, CircleMarker, Popup } from "react-leaflet";
import { fetchOverview } from "../api/client";
import { useFilter } from "../context/FilterContext";
import { ElectoralBadge } from "../components/ElectoralBadge";
import { StatCard } from "../components/StatCard";
import { SentimentBar } from "../components/SentimentBar";
import type { OverviewData } from "../types";

const fmt = (n: number) => n.toLocaleString("pt-BR");

function HeroCard({ hero, deltaPct }: { hero: OverviewData["hero"]; deltaPct: number | null }) {
  if (!hero) return null;
  const isUp = deltaPct !== null && deltaPct >= 0;
  return (
    <div className="bg-white rounded-xl shadow-card border-l-[6px] border-rn-primary p-5 mb-6">
      <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-1">
        🏆 Em destaque · últimos 7 dias
      </p>
      <div className="flex items-baseline gap-3 flex-wrap">
        <span className="text-2xl font-extrabold text-rn-dark">{hero.person_name}</span>
        <span className="text-sm text-gray-500">{hero.person_party ?? "sem partido"}</span>
      </div>
      <div className="flex items-baseline gap-3 mt-2 flex-wrap">
        <span className="text-4xl font-extrabold text-rn-primary leading-none">
          {fmt(hero.mentions)}
        </span>
        <span className="text-sm text-gray-400">menções na semana</span>
        {deltaPct !== null && (
          <span className={`text-sm font-semibold ${isUp ? "text-sent-fav" : "text-sent-alert"}`}>
            {isUp ? "▲" : "▼"} {deltaPct > 0 ? "+" : ""}{deltaPct.toFixed(0)}% vs semana anterior
          </span>
        )}
      </div>
    </div>
  );
}

export function Overview() {
  const { days } = useFilter();
  const { data, isLoading, isError } = useQuery<OverviewData>({
    queryKey: ["overview", days],
    queryFn: () => fetchOverview(days),
    staleTime: 5 * 60_000,
  });

  if (isLoading) return <div className="animate-pulse text-gray-400 py-20 text-center">Carregando dados...</div>;
  if (isError || !data) return <div className="text-sent-alert py-20 text-center">Erro ao carregar dados.</div>;

  const { phase, days_to_first_round, freshness, hero, kpis, map_data, candidates, anomalies } = data;

  const deltaPct =
    hero && hero.prev_mentions && hero.prev_mentions > 0
      ? ((hero.mentions - hero.prev_mentions) / hero.prev_mentions) * 100
      : null;

  return (
    <div>
      {/* Header */}
      <div className="mb-5">
        <h1 className="page-title">Sala de Comando</h1>
        <p className="page-subtitle">O que está acontecendo no Rio Grande do Norte nas eleições de 2026.</p>
        <div className="flex items-center gap-3 mt-3 flex-wrap">
          <ElectoralBadge phase={phase} />
          {days_to_first_round != null && days_to_first_round > 0 && (
            <span className="text-sm text-gray-500">
              <strong className="text-rn-dark">{days_to_first_round}</strong> dias até o 1º turno (04/10/2026)
            </span>
          )}
          <span className="text-xs text-gray-400 ml-auto">🕒 {freshness}</span>
        </div>
      </div>

      {/* Hero */}
      <HeroCard hero={hero} deltaPct={deltaPct} />

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
        <StatCard label="Menções totais"       value={kpis.total}    prev={kpis.prev_total}   />
        <StatCard label="Na imprensa"          value={kpis.rss}      prev={kpis.prev_rss}     />
        <StatCard label="Nas redes sociais"    value={kpis.social}   prev={kpis.prev_social}  />
        <StatCard label="Pessoas monitoradas"  value={kpis.persons}  prev={kpis.prev_persons} />
        <StatCard label="Picos detectados"     value={kpis.anomalies} accent="border-sent-warn" />
      </div>

      {/* Map + Candidates */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 mb-6">
        <div className="lg:col-span-2 section-card">
          <p className="section-title">📍 Onde se fala mais no RN</p>
          {map_data.length > 0 ? (
            <MapContainer
              center={[-5.8, -36.5]}
              zoom={6}
              style={{ height: 400 }}
              scrollWheelZoom={false}
            >
              <TileLayer
                attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              />
              {map_data.map((c) => (
                <CircleMarker
                  key={c.city}
                  center={[c.latitude, c.longitude]}
                  radius={Math.min(8 + Math.sqrt(c.mentions) * 1.2, 40)}
                  pathOptions={{ color: "#004A1C", fillColor: "#009B3A", fillOpacity: 0.65, weight: 1.5 }}
                >
                  <Popup>
                    <strong>{c.city}</strong><br />
                    {fmt(c.mentions)} menções<br />
                    Prefeito(a): {c.mayor}
                  </Popup>
                </CircleMarker>
              ))}
            </MapContainer>
          ) : (
            <div className="h-64 flex items-center justify-center text-gray-400 text-sm">
              Sem dados de cidades disponíveis.
            </div>
          )}
        </div>

        <div className="section-card overflow-y-auto max-h-[480px]">
          <p className="section-title">🏛️ Candidatos em destaque</p>
          <div className="space-y-3">
            {candidates.map((c) => (
              <div key={c.person_name} className="pb-3 border-b border-gray-100 last:border-0">
                <div className="flex items-start justify-between">
                  <div>
                    <p className="font-semibold text-sm text-rn-dark leading-tight">{c.person_name}</p>
                    <p className="text-xs text-gray-400">{c.person_party ?? "sem partido"}</p>
                  </div>
                  <span className="text-xl font-extrabold text-rn-primary leading-none">
                    {fmt(c.mentions)}
                  </span>
                </div>
                <SentimentBar fav={c.fav} warn={c.warn} alert={c.alert} />
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Anomalies */}
      {anomalies.length > 0 && (
        <div className="section-card">
          <p className="section-title">⚡ Picos de menções recentes</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {anomalies.map((a, i) => {
              const isHigh = a.zscore >= 3.5;
              return (
                <div key={i} className="rounded-lg border border-gray-100 bg-gray-50 p-3">
                  <div className="flex justify-between items-start">
                    <div>
                      <p className="font-semibold text-sm text-gray-800">{a.person_name}</p>
                      <p className="text-xs text-gray-400">
                        {new Date(a.day).toLocaleDateString("pt-BR")} · {fmt(a.mentions)} menções
                      </p>
                    </div>
                    <span className="text-xl">{isHigh ? "🔴" : "🟡"}</span>
                  </div>
                  <p className="text-xs text-gray-500 mt-1">
                    Intensidade {isHigh ? "muito alta" : "alta"} (z={a.zscore.toFixed(1)})
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
