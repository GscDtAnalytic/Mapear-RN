import { NavLink } from "react-router-dom";
import { useFilter } from "../context/FilterContext";

const NAV = [
  { to: "/",             icon: "⚡", label: "Sala de Comando" },
  { to: "/candidatos",   icon: "🏆", label: "A Corrida"       },
  { to: "/evolucao",     icon: "📈", label: "Evolução"        },
  { to: "/mapa",         icon: "🗺️", label: "O Mapa"          },
  { to: "/alertas",      icon: "⚠️", label: "Alertas"         },
  { to: "/inteligencia", icon: "🧠", label: "Inteligência"    },
];

const PERIOD_OPTIONS = [
  { label: "7 dias",  value: 7   },
  { label: "14 dias", value: 14  },
  { label: "30 dias", value: 30  },
  { label: "90 dias", value: 90  },
  { label: "180 dias",value: 180 },
];

interface SidebarProps {
  mobileOpen: boolean;
  onClose: () => void;
}

export function Sidebar({ mobileOpen, onClose }: SidebarProps) {
  const { days, setDays } = useFilter();

  return (
    <>
      {/* Backdrop — só no mobile, quando o drawer está aberto */}
      {mobileOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-30 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 w-56 flex flex-col z-40 transition-transform duration-200
                    lg:translate-x-0 ${mobileOpen ? "translate-x-0" : "-translate-x-full"}`}
        style={{ background: "linear-gradient(180deg, #006B28 0%, #003015 100%)" }}>

      {/* Brand */}
      <div className="px-5 pt-6 pb-4 border-b border-white/10">
        <div className="flex items-center gap-2">
          <span className="text-2xl">🗺️</span>
          <div>
            <p className="text-white font-extrabold text-sm leading-tight">Mapear-RN</p>
            <p className="text-green-300 text-xs leading-tight">Eleições 2026</p>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
        {NAV.map(({ to, icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            onClick={onClose}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive
                  ? "bg-white/20 text-white"
                  : "text-green-100 hover:bg-white/10 hover:text-white"
              }`
            }
          >
            <span className="text-base">{icon}</span>
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      {/* Period filter */}
      <div className="px-4 py-4 border-t border-white/10">
        <p className="text-green-300 text-xs font-semibold uppercase tracking-wide mb-2">
          Período
        </p>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="w-full bg-white/10 text-white text-sm rounded-lg px-3 py-2 border border-white/20
                     focus:outline-none focus:border-white/50 cursor-pointer"
        >
          {PERIOD_OPTIONS.map((o) => (
            <option key={o.value} value={o.value} className="bg-rn-dark text-white">
              {o.label}
            </option>
          ))}
        </select>
      </div>

      {/* Footer */}
      <div className="px-4 py-3 border-t border-white/10">
        <p className="text-green-400 text-xs">Atualiza diariamente às 09h</p>
      </div>
      </aside>
    </>
  );
}
