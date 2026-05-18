const PHASES: Record<string, { label: string; color: string }> = {
  pre_campaign:    { label: "Pré-campanha",       color: "bg-amber-100 text-amber-800 border-amber-300" },
  campaign_1st:   { label: "Campanha · 1º turno", color: "bg-green-100 text-green-800 border-green-300" },
  between_rounds: { label: "Entre turnos",        color: "bg-blue-100 text-blue-800 border-blue-300" },
  campaign_2nd:   { label: "Campanha · 2º turno", color: "bg-rn-primary text-white border-rn-dark" },
  post_election:  { label: "Pós-eleição",         color: "bg-gray-100 text-gray-700 border-gray-300" },
  none:           { label: "Período ordinário",   color: "bg-gray-100 text-gray-600 border-gray-200" },
};

export function ElectoralBadge({ phase }: { phase: string }) {
  const { label, color } = PHASES[phase] ?? PHASES.none;
  return (
    <span className={`inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold border ${color}`}>
      {label}
    </span>
  );
}
