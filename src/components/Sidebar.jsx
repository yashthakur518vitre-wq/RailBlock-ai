import { NavLink } from "react-router-dom";
import {
  LayoutDashboard,
  ClipboardList,
  CalendarDays,
  Sparkles,
  BarChart3,
  Settings,
  TrainFront,
  ChevronLeft,
} from "lucide-react";

const menuItems = [
  {
    name: "Dashboard",
    icon: LayoutDashboard,
    path: "/",
  },
  {
    name: "Maintenance Tasks",
    icon: ClipboardList,
    path: "/tasks",
  },
  {
    name: "Block Planning",
    icon: CalendarDays,
    path: "/block-planning",
  },
  {
    name: "AI Recommendations",
    icon: Sparkles,
    path: "/ai-recommendations",
  },
  {
    name: "Analytics",
    icon: BarChart3,
    path: "/analytics",
  },
];

function Sidebar() {
  return (
    <aside className="sidebar">
      {/* Logo */}
      <div className="logo-section">
        <div className="logo-icon">
          <TrainFront size={24} />
        </div>

        <div>
          <h2>RailBlock</h2>
          <span>AI Planning System</span>
        </div>
      </div>

      {/* Navigation */}
      <nav className="nav-menu">
        <p className="menu-label">MAIN MENU</p>

        {menuItems.map((item) => {
          const Icon = item.icon;

          return (
            <NavLink
              key={item.name}
              to={item.path}
              className={({ isActive }) =>
                `nav-item ${isActive ? "active" : ""}`
              }
            >
              <Icon size={20} />
              <span>{item.name}</span>
            </NavLink>
          );
        })}

        <p className="menu-label system-label">SYSTEM</p>

        <button className="nav-item">
          <Settings size={20} />
          <span>Settings</span>
        </button>
      </nav>

      {/* Bottom AI Status */}
      <div className="sidebar-bottom">
        <div className="ai-status">
          <div className="status-dot"></div>

          <div>
            <strong>AI Engine</strong>
            <span>System Operational</span>
          </div>
        </div>

        <button className="collapse-button">
          <ChevronLeft size={18} />
          Collapse Menu
        </button>
      </div>
    </aside>
  );
}

export default Sidebar;