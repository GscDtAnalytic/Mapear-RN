import { Routes, Route } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Overview } from "./pages/Overview";
import { Candidates } from "./pages/Candidates";
import { Trends } from "./pages/Trends";
import { Coverage } from "./pages/Coverage";
import { Alerts } from "./pages/Alerts";
import { Narratives } from "./pages/Narratives";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index         element={<Overview />}   />
        <Route path="candidatos"   element={<Candidates />} />
        <Route path="evolucao"     element={<Trends />}     />
        <Route path="mapa"         element={<Coverage />}   />
        <Route path="alertas"      element={<Alerts />}     />
        <Route path="inteligencia" element={<Narratives />} />
      </Route>
    </Routes>
  );
}
