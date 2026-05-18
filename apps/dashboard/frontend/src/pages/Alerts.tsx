import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, Cell, ScatterChart, Scatter, ReferenceLine,
} from "recharts";
import { fetchSentimentPct, fetchTopics, fetchQuality } from "../api/client";
import { useFilter } from "../context/FilterContext";

const TABS = ["🌡️ Reação do público", "🔥 Temas sensíveis", "🔍 Qualidade do dado"];
const SENT_COLORS: Record<string, string> = { Positivo: "#2E8540", Atenção: "#E08C2B", Crítico: "#C8372D" };
const fmt = (n: number) => n.toLocaleString("pt-BR");

function SentimentPctTab() {
  const { days } = useFilter();
  const [conf, setConf] = useState(0.5);
  const { data = [] } = useQuery<any[]>({
    queryKey: ["sentiment-pct", days, conf],
    queryFn: () => fetchSentimentPct(days, conf),
    staleTime: 5 * 60_000,
  });

  const PT: Record<string, string> = { FAVORABLE: "Positivo", WARNING: "Atenção", ALERT: "Crítico" };

  // compute percentages
  const totals: Record<string, number> = {};
  for (const r of data) totals[r.person_name] = (totals[r.person_name] ?? 0) + r.n;
  const pctData = data.map((r: any) => ({
    ...r, label: PT[r.sentiment_label] ?? r.sentiment_label,
    pct: totals[r.person_name] > 0 ? (r.n / totals[r.person_name]) * 100 : 0,
  }));

  // pivot for stacked bar
  const persons = [...new Set(pctData.map((r: any) => r.person_name))] as string[];
  const alertPct = (p: string) =>
    pctData.find((r: any) => r.person_name === p && r.label === "Crítico")?.pct ?? 0;
  const sorted = [...persons].sort((a, b) => alertPct(b) - alertPct(a));
  const chartData = sorted.map((p) => {
    const row: Record<string, any> = { name: p };
    for (const r of pctData.filter((r: any) => r.person_name === p)) {
      row[r.label] = r.pct;
    }
    return row;
  });

  return (
    <div className="section-card">
      <div className="flex items-center gap-4 mb-4">
        <label className="text-sm font-medium text-gray-700 whitespace-nowrap">
          Confiança mínima: <strong>{conf.toFixed(2)}</strong>
        </label>
        <input type="range" min={0} max={1} step={0.05} value={conf}
          onChange={(e) => setConf(Number(e.target.value))}
          className="flex-1 accent-rn-primary" />
      </div>
      {!chartData.length ? (
        <div className="py-8 text-center text-gray-400 text-sm">
          Sem posts classificados acima do nível de confiança. Tente baixar o limite ou ampliar o período.
        </div>
      ) : (
        <>
          <p className="section-title">Distribuição de tom por candidato (ordenado por % crítico)</p>
          <ResponsiveContainer width="100%" height={Math.max(280, 48 * chartData.length)}>
            <BarChart data={chartData} layout="vertical" margin={{ left: 20, right: 30 }}>
              <XAxis type="number" domain={[0, 100]} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 11 }} />
              <YAxis dataKey="name" type="category" width={130} tick={{ fontSize: 11 }} />
              <Tooltip formatter={(v: number) => `${v.toFixed(1)}%`} />
              <Legend />
              <Bar dataKey="Crítico"  stackId="a" fill={SENT_COLORS.Crítico} />
              <Bar dataKey="Atenção"  stackId="a" fill={SENT_COLORS.Atenção} />
              <Bar dataKey="Positivo" stackId="a" fill={SENT_COLORS.Positivo} radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
          <p className="text-xs text-gray-400 mt-2">
            Barra 100% proporcional. Topo = maior proporção de crítico → prioridade de atenção.
          </p>
        </>
      )}
    </div>
  );
}

function TopicsTab() {
  const { days } = useFilter();
  const [mode, setMode] = useState<"warning" | "polarity">("warning");
  const { data = [] } = useQuery<any[]>({
    queryKey: ["topics", days, mode],
    queryFn: () => fetchTopics(days, mode === "polarity" ? "polarity" : "warning"),
    staleTime: 5 * 60_000,
  });

  const maxVal = Math.max(...data.map((r: any) => r.critical_count), 1);
  const barData = [...data].reverse();

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
      <div className="lg:col-span-2 section-card">
        <div className="flex gap-3 mb-4">
          {(["warning", "polarity"] as const).map((m) => (
            <button key={m} onClick={() => setMode(m)}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                mode === m ? "bg-rn-primary text-white" : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}>
              {m === "warning" ? "Tom negativo da imprensa" : "Polaridade muito negativa (< −0.3)"}
            </button>
          ))}
        </div>
        {!barData.length ? (
          <div className="py-8 text-center text-gray-400 text-sm">
            Sem cruzamento entre temas e tom negativo. Tente o outro critério ou amplie a janela.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={420}>
            <BarChart data={barData} layout="vertical" margin={{ left: 20, right: 40 }}>
              <XAxis type="number" tick={{ fontSize: 11 }} />
              <YAxis dataKey="topic_label" type="category" width={130} tick={{ fontSize: 11 }} />
              <Tooltip formatter={(v: number) => fmt(v)} />
              <Bar dataKey="critical_count" name="Menções críticas" radius={[0, 4, 4, 0]}>
                {barData.map((r: any, i) => {
                  const intensity = r.critical_count / maxVal;
                  const color = intensity < 0.4 ? "#FCD34D" : intensity < 0.7 ? "#E08C2B" : "#C8372D";
                  return <Cell key={i} fill={color} />;
                })}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
      <div>
        {data.length > 0 && (
          <div className="section-card text-center">
            <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">Tema mais sensível</p>
            <p className="text-5xl font-extrabold text-sent-alert leading-none">{fmt(data[0].critical_count)}</p>
            <p className="font-bold text-gray-800 mt-2 text-lg">{data[0].topic_label}</p>
            <p className="text-xs text-gray-400 mt-1">menções negativas</p>
          </div>
        )}
        {data.length > 0 && (
          <div className="section-card mt-4">
            <p className="stat-label">Temas com alerta</p>
            <p className="stat-value text-sent-warn">{data.length}</p>
          </div>
        )}
      </div>
    </div>
  );
}

function QualityTab() {
  const { days } = useFilter();
  const [threshold, setThreshold] = useState(0.6);
  const { data = [] } = useQuery<any[]>({
    queryKey: ["quality", days],
    queryFn: () => fetchQuality(days),
    staleTime: 5 * 60_000,
  });

  const inReview = data.filter((r: any) => r.avg_conf < threshold);
  const avgConf = data.length > 0
    ? (data.reduce((s: number, r: any) => s + r.avg_conf, 0) / data.length).toFixed(2)
    : "—";

  return (
    <div className="space-y-4">
      <div className="section-card">
        <div className="flex items-center gap-4 mb-4">
          <label className="text-sm font-medium text-gray-700 whitespace-nowrap">
            Confiança máxima da fila: <strong>{threshold.toFixed(2)}</strong>
          </label>
          <input type="range" min={0.3} max={1} step={0.05} value={threshold}
            onChange={(e) => setThreshold(Number(e.target.value))}
            className="flex-1 accent-rn-primary" />
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-4">
          <div className="stat-card border-l-4 border-sent-warn">
            <p className="stat-label">Na fila de revisão</p>
            <p className="stat-value">{inReview.length}</p>
          </div>
          <div className="stat-card">
            <p className="stat-label">Confiança média geral</p>
            <p className="stat-value">{avgConf}</p>
          </div>
          <div className="stat-card">
            <p className="stat-label">Limite atual</p>
            <p className="stat-value">{threshold.toFixed(2)}</p>
          </div>
        </div>
      </div>

      {data.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div className="lg:col-span-2 section-card">
            <p className="section-title">Volume × confiança média (log scale)</p>
            <ResponsiveContainer width="100%" height={360}>
              <ScatterChart margin={{ left: 10, right: 20 }}>
                <XAxis type="number" dataKey="n" name="Posts" scale="log" domain={["auto","auto"]}
                  label={{ value: "Volume (log)", position: "insideBottom", offset: -5, fontSize: 11 }} tick={{ fontSize: 10 }} />
                <YAxis type="number" dataKey="avg_conf" name="Confiança" domain={[0, 1]}
                  label={{ value: "Confiança", angle: -90, position: "insideLeft", fontSize: 11 }} tick={{ fontSize: 10 }} />
                <Tooltip
                  content={({ payload }) => {
                    if (!payload?.length) return null;
                    const d = payload[0].payload;
                    return (
                      <div className="bg-white border border-gray-200 rounded-lg p-3 text-xs shadow-card">
                        <p className="font-bold">{d.person_name}</p>
                        <p>{d.platform} · {fmt(d.n)} posts</p>
                        <p>Confiança: {d.avg_conf.toFixed(3)}</p>
                      </div>
                    );
                  }}
                />
                <ReferenceLine y={threshold} stroke="#E08C2B" strokeDasharray="4 2"
                  label={{ value: `Limite ${threshold}`, position: "right", fontSize: 10, fill: "#E08C2B" }} />
                <Scatter data={data} fill="#009B3A" opacity={0.75} />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
          <div className="section-card overflow-y-auto max-h-[440px]">
            <p className="section-title">{inReview.length} abaixo de {threshold.toFixed(2)}</p>
            {inReview.length === 0 ? (
              <div className="text-sent-fav text-sm font-medium py-4">✅ Nenhum abaixo do limite.</div>
            ) : (
              <table className="w-full text-xs mt-1">
                <thead>
                  <tr className="text-gray-400 border-b border-gray-100">
                    <th className="text-left pb-2">Pessoa</th>
                    <th className="pb-2">Plataforma</th>
                    <th className="pb-2">Conf.</th>
                  </tr>
                </thead>
                <tbody>
                  {inReview.slice(0, 15).map((r: any, i: number) => (
                    <tr key={i} className="border-b border-gray-50">
                      <td className="py-1.5 font-medium text-rn-dark">{r.person_name}</td>
                      <td className="py-1.5 text-center text-gray-500">{r.platform}</td>
                      <td className="py-1.5 text-center font-bold text-sent-warn">{r.avg_conf.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function Alerts() {
  const [activeTab, setActiveTab] = useState(0);
  return (
    <div>
      <div className="mb-5">
        <h1 className="page-title">Alertas</h1>
        <p className="page-subtitle">Sinais para ficar de olho: reação negativa, temas sensíveis e qualidade dos dados.</p>
      </div>
      <div className="flex gap-1 border-b border-gray-200 mb-5 overflow-x-auto">
        {TABS.map((t, i) => (
          <button key={i} onClick={() => setActiveTab(i)}
            className={`px-4 py-2.5 text-sm whitespace-nowrap transition-colors ${activeTab === i ? "tab-active" : "tab-inactive"}`}>
            {t}
          </button>
        ))}
      </div>
      {activeTab === 0 && <SentimentPctTab />}
      {activeTab === 1 && <TopicsTab />}
      {activeTab === 2 && <QualityTab />}
    </div>
  );
}
