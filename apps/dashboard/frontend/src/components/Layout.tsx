import { useState } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";

export function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="min-h-screen bg-rn-bg">
      <Sidebar mobileOpen={mobileOpen} onClose={() => setMobileOpen(false)} />

      {/* Barra superior — só no mobile/tablet (a sidebar vira drawer) */}
      <header
        className="lg:hidden fixed top-0 inset-x-0 h-14 z-20 flex items-center gap-3 px-4
                   text-white shadow-md"
        style={{ background: "linear-gradient(90deg, #006B28 0%, #004A1C 100%)" }}
      >
        <button
          onClick={() => setMobileOpen(true)}
          aria-label="Abrir menu"
          className="text-2xl leading-none -ml-1 px-1"
        >
          ☰
        </button>
        <span className="text-lg">🗺️</span>
        <span className="font-extrabold text-sm">Mapear-RN</span>
      </header>

      <main className="lg:ml-56 p-4 sm:p-6 pt-[4.5rem] lg:pt-6 overflow-x-hidden">
        <Outlet />
      </main>
    </div>
  );
}
