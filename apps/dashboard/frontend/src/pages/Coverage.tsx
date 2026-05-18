import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, Cell,
} from "recharts";
import { MapContainer, TileLayer, CircleMarker, Popup } from "react-leaflet";
import { fetchCities, fetchFeeds, fetchPlatforms, fetchSchedule } from "../api/client";
import { useFilter } from "../context/FilterContext";

const TABS = ["🗺️ Cidades", "📰 Jornais", "📱 Redes sociais", "🕐 Horários"];
const RN = "#009B3A";
const fmt = (n: number) => n.toLocaleString("pt-BR");

const PLATFORM_COLORS: Record<string, string> = {
  facebook: "#1877F2", instagram: "#E1306C", tiktok: "#010101",
  x: "#1DA1F2", youtube: "#FF0000",
};

const DOW_PT: Record<string, string> = {
  Monday: "Segunda", Tuesday: "Terça", Wednesday: "Quarta",
  Thursday: "Quinta", Friday: "Sexta", Saturday: "Sábado", Sunday: "Domingo",
};
const DOW_ORDER = ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"];

function CitiesTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<any[]>({
    queryKey: ["cities", days],
    queryFn: () => fetchCities(days),
    staleTime: 5 * 60_000,
  });

  const barData = [...data].reverse().slice(-12);
  const hasCoords = data.some((c: any) => c.latitude && c.longitude);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
      <div className="section-card">
        <p className="section-title">Top cidades por menções</p>
        <ResponsiveContainer width="100%" height={380}>
          <BarChart data={barData} layout="vertical" margin={{ left: 20, right: 40 }}>
            <XAxis type="number" tick={{ fontSize: 11 }} />
            <YAxis dataKey="city" type="category" width={100} tick={{ fontSize: 11 }} />
            <Tooltip formatter={(v: number) => fmt(v)} />
            <Bar dataKey="mentions" name="Menções" radius={[0, 4, 4, 0]}>
              {barData.map((_, i) => <Cell key={i} fill={RN} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="section-card">
        <p className="section-title">Mapa de menções — RN</p>
        {hasCoords ? (
          <MapContainer center={[-5.8, -36.5]} zoom={6} style={{ height: 380 }} scrollWheelZoom={false}>
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            />
            {data.filter((c: any) => c.latitude).map((c: any) => (
              <CircleMarker
                key={c.city}
                center={[c.latitude, c.longitude]}
                radius={Math.min(8 + Math.sqrt(c.mentions) * 1.2, 40)}
                pathOptions={{ color: "#004A1C", fillColor: "#009B3A", fillOpacity: 0.65, weight: 1.5 }}
              >
                <Popup>
                  <strong>{c.city}</strong><br />
                  {fmt(c.mentions)} menções<br />
                  {c.mayor && <>Prefeito(a): {c.mayor}</>}
                </Popup>
              </CircleMarker>
            ))}
          </MapContainer>
        ) : (
          <div className="h-64 flex items-center justify-center text-gray-400 text-sm">
            Coordenadas não disponíveis.
          </div>
        )}
      </div>
    </div>
  );
}

function FeedsTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<any[]>({
    queryKey: ["feeds", days],
    queryFn: () => fetchFeeds(days),
    staleTime: 5 * 60_000,
  });

  const total = data.reduce((s: number, r: any) => s + r.articles, 0);
  const barData = [...data].reverse().slice(-15);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
      <div className="lg:col-span-2 section-card">
        <p className="section-title">Jornais e portais com mais cobertura</p>
        <ResponsiveContainer width="100%" height={420}>
          <BarChart data={barData} layout="vertical" margin={{ left: 20, right: 40 }}>
            <XAxis type="number" tick={{ fontSize: 11 }} />
            <YAxis dataKey="source_feed" type="category" width={130} tick={{ fontSize: 11 }} />
            <Tooltip formatter={(v: number) => fmt(v)} />
            <Bar dataKey="articles" name="Notícias" radius={[0, 4, 4, 0]}>
              {barData.map((_, i) => <Cell key={i} fill={RN} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="section-card overflow-y-auto max-h-[460px]">
        <p className="section-title">Participação no total</p>
        <div className="space-y-2">
          {data.slice(0, 10).map((r: any) => {
            const pct = total > 0 ? ((r.articles / total) * 100).toFixed(1) : "0";
            return (
              <div key={r.source_feed} className="flex justify-between items-center py-1.5 border-b border-gray-100">
                <span className="text-sm text-gray-700 truncate flex-1 mr-2">{r.source_feed}</span>
                <div className="flex items-center gap-2">
                  <div className="w-16 bg-gray-100 rounded-full h-1.5">
                    <div className="bg-rn-primary h-1.5 rounded-full" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="text-xs font-bold text-rn-primary whitespace-nowrap">{pct}%</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function PlatformsTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<any[]>({
    queryKey: ["platforms", days],
    queryFn: () => fetchPlatforms(days),
    staleTime: 5 * 60_000,
  });

  const total = data.reduce((s: number, r: any) => s + r.events, 0);
  const barData = [...data].reverse();

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
      <div className="section-card">
        <p className="section-title">Volume por plataforma</p>
        <ResponsiveContainer width="100%" height={300}>
          <BarChart data={barData} layout="vertical" margin={{ left: 20, right: 40 }}>
            <XAxis type="number" tick={{ fontSize: 11 }} />
            <YAxis dataKey="platform" type="category" width={90} tick={{ fontSize: 12 }} />
            <Tooltip formatter={(v: number) => fmt(v)} />
            <Bar dataKey="events" name="Menções" radius={[0, 4, 4, 0]}>
              {barData.map((r: any, i) => (
                <Cell key={i} fill={PLATFORM_COLORS[r.platform] ?? RN} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="section-card">
        <p className="section-title">Distribuição — % do total</p>
        <div className="space-y-3 mt-2">
          {data.map((r: any) => {
            const pct = total > 0 ? Math.round((r.events / total) * 100) : 0;
            const color = PLATFORM_COLORS[r.platform] ?? RN;
            return (
              <div key={r.platform}>
                <div className="flex justify-between text-sm mb-1">
                  <span className="font-semibold capitalize">{r.platform}</span>
                  <span className="font-bold" style={{ color }}>{fmt(r.events)}</span>
                </div>
                <div className="bg-gray-100 rounded-full h-2">
                  <div className="h-2 rounded-full" style={{ width: `${pct}%`, backgroundColor: color }} />
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function ScheduleTab() {
  const { days } = useFilter();
  const { data = [] } = useQuery<any[]>({
    queryKey: ["schedule", days],
    queryFn: () => fetchSchedule(days),
    staleTime: 5 * 60_000,
  });

  if (!data.length) return (
    <div className="section-card py-10 text-center text-gray-400 text-sm">Sem dados para o período.</div>
  );

  const hours = Array.from({ length: 24 }, (_, i) => i);
  const dowList = [...new Set(data.map((r: any) => DOW_PT[r.dow_name] ?? r.dow_name))]
    .sort((a, b) => DOW_ORDER.indexOf(a) - DOW_ORDER.indexOf(b));

  const lookup: Record<string, Record<number, number>> = {};
  let maxVal = 0;
  for (const r of data) {
    const dow = DOW_PT[r.dow_name] ?? r.dow_name;
    if (!lookup[dow]) lookup[dow] = {};
    lookup[dow][r.hour] = r.articles;
    if (r.articles > maxVal) maxVal = r.articles;
  }

  const peakHour = data.reduce((best: any, r: any) => !best || r.articles > best.articles ? r : best, null);

  return (
    <div className="section-card">
      <p className="section-title">Quando o conteúdo é publicado (hora × dia da semana)</p>
      <div className="overflow-x-auto mt-2">
        <table className="w-full text-xs border-separate border-spacing-0.5">
          <thead>
            <tr>
              <th className="text-left pr-3 text-gray-400 font-medium py-1"></th>
              {hours.filter(h => h >= 5 && h <= 23).map((h) => (
                <th key={h} className="text-center text-gray-400 font-medium px-0.5 py-1">{h}h</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {dowList.map((dow) => (
              <tr key={dow}>
                <td className="pr-3 text-gray-600 font-medium py-0.5 whitespace-nowrap">{dow}</td>
                {hours.filter(h => h >= 5 && h <= 23).map((h) => {
                  const v = lookup[dow]?.[h] ?? 0;
                  const intensity = maxVal > 0 ? v / maxVal : 0;
                  const bg = intensity === 0 ? "#f9fafb"
                    : intensity < 0.25 ? "#d1fae5"
                    : intensity < 0.5 ? "#6ee7b7"
                    : intensity < 0.75 ? "#2E8540"
                    : "#004A1C";
                  const text = intensity > 0.5 ? "#fff" : "#374151";
                  return (
                    <td key={h} className="text-center rounded py-1 px-0.5"
                      style={{ backgroundColor: bg, color: text, minWidth: 28 }}
                      title={`${dow} ${h}h: ${v} artigos`}>
                      {v > 0 ? v : ""}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {peakHour && (
        <div className="mt-4 p-3 bg-green-50 border border-green-200 rounded-lg text-sm text-green-800">
          📍 <strong>Pico de publicação:</strong> {DOW_PT[peakHour.dow_name] ?? peakHour.dow_name}, por volta das {peakHour.hour}h.
          Bom momento para programar postagens da campanha — o público já está atento.
        </div>
      )}
    </div>
  );
}

export function Coverage() {
  const [activeTab, setActiveTab] = useState(0);
  return (
    <div>
      <div className="mb-5">
        <h1 className="page-title">O Mapa</h1>
        <p className="page-subtitle">De onde vêm as menções sobre o Rio Grande do Norte.</p>
      </div>
      <div className="flex gap-1 border-b border-gray-200 mb-5 overflow-x-auto">
        {TABS.map((t, i) => (
          <button key={i} onClick={() => setActiveTab(i)}
            className={`px-4 py-2.5 text-sm whitespace-nowrap transition-colors ${activeTab === i ? "tab-active" : "tab-inactive"}`}>
            {t}
          </button>
        ))}
      </div>
      {activeTab === 0 && <CitiesTab />}
      {activeTab === 1 && <FeedsTab />}
      {activeTab === 2 && <PlatformsTab />}
      {activeTab === 3 && <ScheduleTab />}
    </div>
  );
}
