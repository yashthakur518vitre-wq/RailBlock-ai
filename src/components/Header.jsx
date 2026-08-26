import {
  Bell,
  Search,
  ChevronDown,
  CalendarDays,
} from "lucide-react";

function Header() {
  return (
    <header className="header">
      <div className="header-left">
        <h1>Dashboard</h1>
        <p>Monitor maintenance planning and railway infrastructure availability.</p>
      </div>

      <div className="header-right">
        <div className="search-box">
          <Search size={19} />
          <input
            type="text"
            placeholder="Search tasks, corridors..."
          />
        </div>

        <button className="date-button">
          <CalendarDays size={19} />
          <span>Aug 22, 2026</span>
        </button>

        <button className="notification-button">
          <Bell size={20} />
          <span className="notification-dot"></span>
        </button>

        <div className="user-profile">
          <div className="avatar">KS</div>

          <div className="user-info">
            <strong>Control Admin</strong>
            <span>Administrator</span>
          </div>

          <ChevronDown size={18} />
        </div>
      </div>
    </header>
  );
}

export default Header;