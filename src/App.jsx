import { Routes, Route } from "react-router-dom";

import Sidebar from "./components/Sidebar";
import Header from "./components/Header";

import Dashboard from "./pages/Dashboard";
import Tasks from "./pages/Tasks";
import BlockPlanning from "./pages/BlockPlanning";
import AIRecommendations from "./pages/AIRecommendations";
import Analytics from "./pages/Analytics";

import "./App.css";

function App() {
  return (
    <div className="app-container">
      <Sidebar />

      <main className="main-content">
        <Header />

        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/block-planning" element={<BlockPlanning />} />
          <Route path="/ai-recommendations" element={<AIRecommendations />} />
          <Route path="/analytics" element={<Analytics />} />
        </Routes>
      </main>
    </div>
  );
}

export default App;