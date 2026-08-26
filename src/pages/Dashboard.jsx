import {
  AlertTriangle,
  ClipboardCheck,
  Activity,
  CalendarCheck,
  ArrowUpRight,
  Train,
  Clock3,
} from "lucide-react";

import StatCard from "../components/StatCard";
import BlockCard from "../components/BlockCard";

function Dashboard() {
  return (
    <div className="dashboard">

      {/* Welcome section */}
      <section className="welcome-section">
        <div>
          <p className="welcome-label">
            RAILWAY INFRASTRUCTURE CONTROL
          </p>

          <h2>
            Good Morning, <span>Admin</span>
          </h2>

          <p>
            Here's an overview of today's maintenance operations
            and optimized block planning.
          </p>
        </div>

        <button className="generate-button">
          <Activity size={19} />
          Generate AI Block Plan
        </button>
      </section>


      {/* Statistics */}
      <section className="stats-grid">
        <StatCard
          title="Critical Tasks"
          value="12"
          subtitle="Requires immediate attention"
          icon={AlertTriangle}
          trend="+3 today"
          trendType="negative"
        />

        <StatCard
          title="Scheduled Tasks"
          value="86"
          subtitle="Across all departments"
          icon={ClipboardCheck}
          trend="+14%"
          trendType="positive"
        />

        <StatCard
          title="Asset Availability"
          value="96.5%"
          subtitle="Infrastructure operational"
          icon={Activity}
          trend="+1.8%"
          trendType="positive"
        />

        <StatCard
          title="Optimized Blocks"
          value="24"
          subtitle="Planned for this week"
          icon={CalendarCheck}
          trend="18% saved"
          trendType="positive"
        />
      </section>


      {/* Main content grid */}
      <section className="dashboard-grid">

        {/* AI Recommendation */}
        <div className="panel recommendation-panel">
          <div className="panel-header">
            <div>
              <p className="section-label">AI OPTIMIZATION</p>
              <h3>Today's Priority Recommendation</h3>
            </div>

            <span className="live-badge">
              <span></span>
              LIVE
            </span>
          </div>

          <div className="recommendation-content">
            <div className="recommendation-icon">
              <Train size={30} />
            </div>

            <div className="recommendation-text">
              <h4>Combine maintenance activities in Corridor A</h4>

              <p>
                The AI engine detected 3 pending tasks across
                Engineering, Signalling and Traction departments
                that can be completed during a single coordinated block.
              </p>

              <div className="recommendation-stats">
                <div>
                  <strong>3</strong>
                  <span>Departments</span>
                </div>

                <div>
                  <strong>2.5 hrs</strong>
                  <span>Downtime Saved</span>
                </div>

                <div>
                  <strong>Low</strong>
                  <span>Train Impact</span>
                </div>
              </div>
            </div>
          </div>

          <div className="ai-reason">
            <span>AI CONFIDENCE</span>

            <div className="confidence-bar">
              <div className="confidence-progress"></div>
            </div>

            <strong>94%</strong>
          </div>
        </div>


        {/* System overview */}
        <div className="panel system-panel">
          <div className="panel-header">
            <div>
              <p className="section-label">SYSTEM STATUS</p>
              <h3>Infrastructure Overview</h3>
            </div>

            <ArrowUpRight size={20} />
          </div>

          <div className="system-list">
            <div className="system-row">
              <div className="system-name">
                <span className="system-indicator engineering"></span>
                Engineering
              </div>

              <strong>32 Tasks</strong>
            </div>

            <div className="system-row">
              <div className="system-name">
                <span className="system-indicator signalling"></span>
                Signal & Telecom
              </div>

              <strong>28 Tasks</strong>
            </div>

            <div className="system-row">
              <div className="system-name">
                <span className="system-indicator traction"></span>
                Traction Distribution
              </div>

              <strong>26 Tasks</strong>
            </div>
          </div>

          <div className="availability-box">
            <div>
              <p>Overall Availability</p>
              <h2>96.5%</h2>
            </div>

            <div className="availability-circle">
              96%
            </div>
          </div>
        </div>
      </section>


      {/* Recommended blocks */}
      <section className="blocks-section">
        <div className="section-heading">
          <div>
            <p className="section-label">OPTIMIZED SCHEDULE</p>
            <h3>Recommended Maintenance Blocks</h3>
          </div>

          <button className="view-all-button">
            View Full Plan →
          </button>
        </div>

        <div className="blocks-grid">
          <BlockCard
            corridor="Corridor A"
            time="Today • 02:00 PM – 05:00 PM"
            departments={[
              "Engineering",
              "Signal & Telecom",
              "Traction",
            ]}
            priority="94"
            impact="Low"
          />

          <BlockCard
            corridor="Corridor C"
            time="Today • 11:30 AM – 01:30 PM"
            departments={[
              "Engineering",
              "Signal & Telecom",
            ]}
            priority="87"
            impact="Medium"
          />

          <BlockCard
            corridor="Corridor B"
            time="Tomorrow • 01:00 PM – 04:00 PM"
            departments={[
              "Engineering",
              "Traction",
            ]}
            priority="81"
            impact="Low"
          />
        </div>
      </section>


      {/* Footer info */}
      <div className="dashboard-footer">
        <div>
          <Clock3 size={17} />
          Last AI optimization run: 5 minutes ago
        </div>

        <span>RailBlock AI v1.0</span>
      </div>

    </div>
  );
}

export default Dashboard;