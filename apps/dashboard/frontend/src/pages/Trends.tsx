import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, ScatterChart, Scatter, ReferenceArea,
  AreaChart, Area,
} from "recharts";
import { fetchWeekly, fetchDailySentiment, fetchSpikes } from "../api/client";
import { useFilter } from "../context/FilterContext";
import type { WeeklyMention } from "../types";

const TABS = ["📈 Linha do tempo", "😊 Reação do público", "🚀 Picos de menções"];
const SENT_COLORS: Record<string, string> = { FAVORABLE: "#2E8540", WARNING: "#E08C2B", ALERT: "#C8372D" };
const SENT_PT: Record<string, string> = { FAVORABLE: "Positivo", WARNING: "Atenção", ALERT: "Crítico" };

const PHASE_FILLS: Record<string, string> = {
  pre_campaign:    "rgba(224,140,43,0.12)",
  campaign_1st:   "rgba(46,133,64,0.12)",
  between_rounds: "rgba(59,130,246,0.10)",
  campaign_2nd:   "rgba(1,50,32,0.18)",
  post_election:  "rgba(107,114,128,0.10)",
};
const PHASE_PT: Record<string, string> = {
  pre_campaign:    "Pré-campanha",
  campaign_1st:   "Campanha 1º turno",
  campaign_2nd:   "Campanha 2º turno",
  between_rounds: "Entre turnos",
  post_election:  "Pós-eleição",
};

const LINE_PALETTE = ["#009B3A","#2563EB","#D97706","#7C3AED","#DB2777","#0891B2","#65A30D"];

function fmt(n: number) { return n.toLocaleString("pt-BR"); }

function WeeklyTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<WeeklyMention[]>({
    queryKey: ["weekly", days],
    queryFn: () => fetchWeekly(Math.max(days, 90)),
    staleTime: 5 * 60_000,
  });

  const persons = [...new Set(data.map((r) => r.person_name))];
  // pivot by week_start
  const weeks = [...new Set(data.map((r) => r.week_start))].sort();
  const chartData = weeks.map((w) => {
    const row: Record<string, any> = { week: w.slice(0, 10) };
    for (const p of persons) {
      const found = data.find((r) => r.week_start === w && r.person_name === p);
      row[p] = found?.mentions_total ?? 0;
    }
    return row;
  });

  // phase bands
  const phaseChanges: Array<{ x0: string; x1: string; phase: string }> = [];
  let cur: string | null = null;
  let x0: string = "";
  for (const r of data) {
    if (r.electoral_phase !== cur) {
      if (cur !== null && cur !== "none") {
        phaseChanges.push({ x0, x1: r.week_start.slice(0, 10), phase: cur });
      }
      cur = r.electoral_phase;
      x0 = r.week_start.slice(0, 10);
    }
  }

  return (
    <div className="section-card">
      <p className="section-title">Menções semanais por candidato a governador</p>
      <ResponsiveContainer width="100%" height={420}>
        <LineChart data={chartData} margin={{ left: 0, right: 20 }}>
          <XAxis dataKey="week" tick={{ fontSize: 10 }} tickFormatter={(v) => v.slice(5)} />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip formatter={(v: number) => fmt(v)} />
          <Legend />
          {phaseChanges.map((p, i) => (
            <ReferenceArea
              key={i}
              x1={p.x0} x2={p.x1}
              fill={PHASE_FILLS[p.phase] ?? "transparent"}
              stroke="none"
              label={{ value: PHASE_PT[p.phase] ?? "", position: "insideTopLeft", fontSize: 9, fill: "#888" }}
            />
          ))}
          {persons.map((p, i) => (
            <Line
              key={p}
              type="monotone"
              dataKey={p}
              stroke={LINE_PALETTE[i % LINE_PALETTE.length]}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <p className="text-xs text-gray-400 mt-2">
        Fundo colorido = fase do calendário eleitoral TSE. Definido pela Lei 9.504/97.
      </p>
    </div>
  );
}

function DailySentimentTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<any[]>({
    queryKey: ["daily-sentiment", days],
    queryFn: () => fetchDailySentiment(days),
    staleTime: 5 * 60_000,
  });

  if (!data.length) return (
    <div className="section-card py-10 text-center text-gray-400 text-sm">
      Ainda não há dados de redes sociais classificados. Tente ampliar o período (≥ 30 dias).
    </div>
  );

  const persons = [...new Set(data.map((r: any) => r.person_name))];

  return (
    <div className="space-y-4">
      {persons.map((person) => {
        const personData = data.filter((r: any) => r.person_name === person);
        const days_list = [...new Set(personData.map((r: any) => r.day as string))].sort();
        const chartData = days_list.map((day) => {
          const row: Record<string, any> = { day: day.slice(5) };
          for (const label of ["FAVORABLE", "WARNING", "ALERT"]) {
            const found = personData.find((r: any) => r.day === day && r.sentiment_label === label);
            row[label] = found?.n ?? 0;
          }
          return row;
        });
        return (
          <div key={person} className="section-card">
            <p className="section-title font-bold text-rn-dark">{person}</p>
            <ResponsiveContainer width="100%" height={160}>
              <AreaChart data={chartData} margin={{ left: 0, right: 10 }}>
                <XAxis dataKey="day" tick={{ fontSize: 9 }} />
                <YAxis tick={{ fontSize: 10 }} />
                <Tooltip formatter={(v: number) => fmt(v)} />
                <Legend formatter={(v) => SENT_PT[v] ?? v} />
                <Area type="monotone" dataKey="FAVORABLE" stackId="1" stroke={SENT_COLORS.FAVORABLE} fill={SENT_COLORS.FAVORABLE} fillOpacity={0.6} />
                <Area type="monotone" dataKey="WARNING"   stackId="1" stroke={SENT_COLORS.WARNING}   fill={SENT_COLORS.WARNING}   fillOpacity={0.6} />
                <Area type="monotone" dataKey="ALERT"     stackId="1" stroke={SENT_COLORS.ALERT}     fill={SENT_COLORS.ALERT}     fillOpacity={0.6} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        );
      })}
    </div>
  );
}

function SpikesTab() {
  const { days } = useFilter();
  const [zMin, setZMin] = useState(2.0);
  const { data = [] } = useQuery<any[]>({
    queryKey: ["spikes", days, zMin],
    queryFn: () => fetchSpikes(days, zMin),
    staleTime: 5 * 60_000,
  });

  return (
    <div className="space-y-4">
      <div className="section-card">
        <div className="flex items-center gap-4 mb-4">
          <label className="text-sm font-medium text-gray-700 whitespace-nowrap">
            Sensibilidade (z-score mínimo): <strong>{zMin}</strong>
          </label>
          <input
            type="range" min={2} max={5} step={0.5} value={zMin}
            onChange={(e) => setZMin(Number(e.target.value))}
            className="flex-1 accent-rn-primary"
          />
        </div>
        {data.length === 0 ? (
          <div className="py-8 text-center text-sent-fav font-medium">
            ✅ Nenhum pico detectado com os critérios atuais. Cenário estável.
          </div>
        ) : (
          <>
            <p className="section-title">Picos × tempo (bolha = intensidade)</p>
            <ResponsiveContainer width="100%" height={300}>
              <ScatterChart margin={{ left: 10, right: 20 }}>
                <XAxis dataKey="day" name="Data" tick={{ fontSize: 10 }}
                  tickFormatter={(v) => typeof v === "string" ? v.slice(5) : v} />
                <YAxis dataKey="person_name" name="Candidato" type="category" width={130} tick={{ fontSize: 11 }} />
                <Tooltip
                  content={({ payload }) => {
                    if (!payload?.length) return null;
                    const d = payload[0].payload;
                    return (
                      <div className="bg-white border border-gray-200 rounded-lg p-3 text-xs shadow-card">
                        <p className="font-bold">{d.person_name}</p>
                        <p>{new Date(d.day).toLocaleDateString("pt-BR")}</p>
                        <p>{fmt(d.mentions)} menções · z={d.zscore.toFixed(2)}</p>
                      </div>
                    );
                  }}
                />
                <Scatter
                  data={data}
                  fill="#E08C2B"
                  opacity={0.75}
                  shape={(props: any) => {
                    const r = Math.min(6 + props.payload.zscore * 3, 26);
                    return <circle cx={props.cx} cy={props.cy} r={r} fill={props.payload.zscore >= 3.5 ? "#C8372D" : "#E08C2B"} opacity={0.75} />;
                  }}
                />
              </ScatterChart>
            </ResponsiveContainer>
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-xs text-gray-500 border-b border-gray-100">
                  <tr>
                    <th className="text-left py-2">Pessoa</th>
                    <th className="py-2">Data</th>
                    <th className="py-2">Menções</th>
                    <th className="py-2">Média habitual</th>
                    <th className="py-2">Intensidade</th>
                  </tr>
                </thead>
                <tbody>
                  {data.slice(0, 20).map((r: any, i: number) => (
                    <tr key={i} className="border-b border-gray-50">
                      <td className="py-2 font-medium text-rn-dark">{r.person_name}</td>
                      <td className="py-2 text-center text-gray-500">{new Date(r.day).toLocaleDateString("pt-BR")}</td>
                      <td className="py-2 text-center font-bold">{fmt(r.mentions)}</td>
                      <td className="py-2 text-center text-gray-500">{r.rolling_mean_30d?.toFixed(1) ?? "—"}</td>
                      <td className="py-2 text-center">
                        <span className={`font-bold ${r.zscore >= 3.5 ? "text-sent-alert" : "text-sent-warn"}`}>
                          {r.zscore.toFixed(1)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function Trends() {
  const [activeTab, setActiveTab] = useState(0);
  return (
    <div>
      <div className="mb-5">
        <h1 className="page-title">Evolução</h1>
        <p className="page-subtitle">Como a narrativa política do RN evoluiu ao longo do tempo.</p>
      </div>
      <div className="flex gap-1 border-b border-gray-200 mb-5 overflow-x-auto">
        {TABS.map((t, i) => (
          <button key={i} onClick={() => setActiveTab(i)}
            className={`px-4 py-2.5 text-sm whitespace-nowrap transition-colors ${activeTab === i ? "tab-active" : "tab-inactive"}`}>
            {t}
          </button>
        ))}
      </div>
      {activeTab === 0 && <WeeklyTab />}
      {activeTab === 1 && <DailySentimentTab />}
      {activeTab === 2 && <SpikesTab />}
    </div>
  );
}
