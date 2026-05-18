interface Props {
  label: string;
  value: string | number;
  prev?: number | null;
  help?: string;
  accent?: string;
}

function delta(curr: number, prev: number | null | undefined): string | null {
  if (!prev || prev === 0) return null;
  const pct = ((curr - prev) / prev) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(0)}%`;
}

export function StatCard({ label, value, prev, help, accent = "border-rn-primary" }: Props) {
  const numVal = typeof value === "number" ? value : null;
  const d = numVal !== null ? delta(numVal, prev) : null;
  const isUp = d !== null && d.startsWith("+");
  const fmt = (n: number) => n.toLocaleString("pt-BR");

  return (
    <div className={`stat-card border-l-4 ${accent}`} title={help}>
      <p className="stat-label">{label}</p>
      <p className="stat-value">{typeof value === "number" ? fmt(value) : value}</p>
      {d && (
        <p className={isUp ? "stat-delta-up" : "stat-delta-down"}>
          {isUp ? "▲" : "▼"} {d} vs período anterior
        </p>
      )}
    </div>
  );
}
