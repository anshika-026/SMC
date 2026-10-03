import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import ErrorBoundary from "./components/ErrorBoundary";
import AppShell from "./layouts/AppShell";
import Footfall from "./pages/Footfall";
import FootfallUat from "./pages/FootfallUat";

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/footfall" element={<Footfall />} />
            <Route path="/footfall-uat" element={<FootfallUat />} />
          </Route>
          <Route path="*" element={<Navigate to="/footfall" replace />} />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
