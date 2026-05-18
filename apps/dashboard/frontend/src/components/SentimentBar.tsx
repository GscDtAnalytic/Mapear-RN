interface Props {
  fav: number;
  warn: number;
  alert: number;
}

export function SentimentBar({ fav, warn, alert }: Props) {
  const total = fav + warn + alert;
  if (total === 0) return null;
  const fp = Math.round((fav / total) * 100);
  const wp = Math.round((warn / total) * 100);
  const ap = 100 - fp - wp;
  return (
    <div className="mt-2">
      <div className="flex rounded-full overflow-hidden h-2 gap-px">
        {fp > 0 && <div className="bg-sent-fav" style={{ width: `${fp}%` }} />}
        {wp > 0 && <div className="bg-sent-warn" style={{ width: `${wp}%` }} />}
        {ap > 0 && <div className="bg-sent-alert" style={{ width: `${ap}%` }} />}
      </div>
      <div className="flex gap-3 mt-1 text-xs text-gray-500">
        <span className="text-sent-fav font-medium">{fp}% positivo</span>
        <span className="text-sent-warn font-medium">{wp}% atenção</span>
        <span className="text-sent-alert font-medium">{ap}% crítico</span>
      </div>
    </div>
  );
}
