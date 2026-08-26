import { useMemo } from "react";
import {
  BarChart3,
  TrendingDown,
  ShieldCheck,
  Wrench,
  Sparkles,
  Activity,
} from "lucide-react";

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Legend,
} from "recharts";

import { maintenanceTasks } from "../data/maintenanceData";
import { generateBlockPlan } from "../utils/blockPlanner";

function Analytics() {
  const analytics = useMemo(() => {
    const blockPlans = generateBlockPlan(maintenanceTasks);

    // -----------------------------
    // TASKS BY DEPARTMENT
    // -----------------------------
    const departmentCounts = {};

    maintenanceTasks.forEach((task) => {
      if (!departmentCounts[task.department]) {
        departmentCounts[task.department] = 0;
      }

      departmentCounts[task.department]++;
    });

    const departmentData = Object.entries(
      departmentCounts
    ).map(([name, value]) => ({
      name:
        name === "Signal & Telecommunication"
          ? "S&T"
          : name === "Traction Distribution"
          ? "Traction"
          : "Engineering",
      value,
    }));

    // -----------------------------
    // TASKS BY PRIORITY
    // -----------------------------
    const priorities = [
      "Critical",
      "High",
      "Medium",
      "Low",
    ];

    const priorityData = priorities.map(
      (priority) => ({
        name: priority,
        value: maintenanceTasks.filter(
          (task) => task.priority === priority
        ).length,
      })
    );

    // -----------------------------
    // DOWNTIME COMPARISON
    // -----------------------------
    const totalSeparateDowntime =
      blockPlans.reduce(
        (total, block) =>
          total + block.separateBlockHours,
        0
      );

    const totalOptimizedDowntime =
      blockPlans.reduce(
        (total, block) =>
          total + block.blockDuration,
        0
      );

    const totalDowntimeSaved =
      totalSeparateDowntime -
      totalOptimizedDowntime;

    const downtimeData = [
      {
        name: "Before AI",
        hours: Number(
          totalSeparateDowntime.toFixed(1)
        ),
      },
      {
        name: "After AI",
        hours: Number(
          totalOptimizedDowntime.toFixed(1)
        ),
      },
    ];

    // -----------------------------
    // PERFORMANCE METRICS
    // -----------------------------
    const efficiencyImprovement =
      totalSeparateDowntime > 0
        ? (
            (totalDowntimeSaved /
              totalSeparateDowntime) *
            100
          ).toFixed(1)
        : 0;

    const criticalTasks =
      maintenanceTasks.filter(
        (task) => task.priority === "Critical"
      ).length;

    const overdueTasks =
      maintenanceTasks.filter(
        (task) => task.status === "Overdue"
      ).length;

    const averageAIConfidence =
      blockPlans.length > 0
        ? Math.round(
            blockPlans.reduce(
              (total, block) =>
                total + block.aiConfidence,
              0
            ) / blockPlans.length
          )
        : 0;

    return {
      departmentData,
      priorityData,
      downtimeData,
      totalSeparateDowntime,
      totalOptimizedDowntime,
      totalDowntimeSaved,
      efficiencyImprovement,
      criticalTasks,
      overdueTasks,
      averageAIConfidence,
      totalTasks: maintenanceTasks.length,
    };
  }, []);

  return (
    <div className="analytics-page">

      {/* PAGE HEADER */}

      <div className="page-heading analytics-heading">
        <div>
          <p className="welcome-label">
            PERFORMANCE & OPTIMIZATION INSIGHTS
          </p>

          <h1>Analytics Dashboard</h1>

          <p>
            Monitor maintenance workload, priority
            distribution, and the impact of AI-powered
            block optimization on railway infrastructure
            availability.
          </p>
        </div>

        <div className="analytics-status">
          <Activity size={17} />
          LIVE ANALYTICS
        </div>
      </div>

      {/* KPI CARDS */}

      <div className="analytics-kpi-grid">

        <div className="analytics-kpi-card">
          <div className="summary-icon blue">
            <Wrench size={22} />
          </div>

          <div>
            <span>Total Maintenance Tasks</span>
            <h2>{analytics.totalTasks}</h2>
          </div>
        </div>

        <div className="analytics-kpi-card">
          <div className="summary-icon red">
            <ShieldCheck size={22} />
          </div>

          <div>
            <span>Critical Tasks</span>
            <h2>{analytics.criticalTasks}</h2>
          </div>
        </div>

        <div className="analytics-kpi-card">
          <div className="summary-icon green">
            <TrendingDown size={22} />
          </div>

          <div>
            <span>Downtime Reduction</span>
            <h2>{analytics.efficiencyImprovement}%</h2>
          </div>
        </div>

        <div className="analytics-kpi-card">
          <div className="summary-icon purple">
            <Sparkles size={22} />
          </div>

          <div>
            <span>Average AI Confidence</span>
            <h2>{analytics.averageAIConfidence}%</h2>
          </div>
        </div>

      </div>

      {/* CHARTS */}

      <div className="analytics-chart-grid">

        {/* DEPARTMENT CHART */}

        <div className="analytics-chart-card">
          <div className="chart-header">
            <div>
              <h3>Tasks by Department</h3>
              <p>
                Maintenance workload across departments
              </p>
            </div>

            <BarChart3 size={19} />
          </div>

          <div className="chart-container">
            <ResponsiveContainer
              width="100%"
              height={280}
            >
              <BarChart
                data={analytics.departmentData}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                />

                <XAxis dataKey="name" />

                <YAxis allowDecimals={false} />

                <Tooltip />

                <Bar
                  dataKey="value"
                  radius={[6, 6, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* PRIORITY CHART */}

        <div className="analytics-chart-card">
          <div className="chart-header">
            <div>
              <h3>Tasks by Priority</h3>
              <p>
                Risk and urgency distribution
              </p>
            </div>

            <ShieldCheck size={19} />
          </div>

          <div className="chart-container">
            <ResponsiveContainer
              width="100%"
              height={280}
            >
              <PieChart>
                <Pie
                  data={analytics.priorityData}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  outerRadius={85}
                  label
                >
                  {analytics.priorityData.map(
                    (entry, index) => (
                      <Cell key={index} />
                    )
                  )}
                </Pie>

                <Tooltip />

                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>

      </div>

      {/* DOWNTIME IMPACT */}

      <div className="downtime-card">

        <div className="chart-header">
          <div>
            <h3>AI Optimization Impact</h3>

            <p>
              Comparison between decentralized
              maintenance blocks and coordinated
              AI-generated block schedules.
            </p>
          </div>

          <TrendingDown size={20} />
        </div>

        <div className="downtime-content">

          <div className="downtime-chart">
            <ResponsiveContainer
              width="100%"
              height={280}
            >
              <BarChart
                data={analytics.downtimeData}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                />

                <XAxis dataKey="name" />

                <YAxis
                  label={{
                    value: "Hours",
                    angle: -90,
                    position: "insideLeft",
                  }}
                />

                <Tooltip />

                <Bar
                  dataKey="hours"
                  radius={[6, 6, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>

          <div className="impact-summary">

            <div className="impact-item">
              <span>Separate Blocks</span>

              <strong>
                {analytics.totalSeparateDowntime.toFixed(
                  1
                )}{" "}
                hrs
              </strong>
            </div>

            <div className="impact-item">
              <span>Optimized Blocks</span>

              <strong>
                {analytics.totalOptimizedDowntime.toFixed(
                  1
                )}{" "}
                hrs
              </strong>
            </div>

            <div className="impact-item highlight">
              <span>Total Downtime Saved</span>

              <strong>
                {analytics.totalDowntimeSaved.toFixed(
                  1
                )}{" "}
                hrs
              </strong>
            </div>

            <div className="impact-item highlight">
              <span>Efficiency Improvement</span>

              <strong>
                {analytics.efficiencyImprovement}%
              </strong>
            </div>

          </div>

        </div>

      </div>

      {/* INSIGHT PANEL */}

      <div className="analytics-insight">

        <div className="insight-icon">
          <Sparkles size={23} />
        </div>

        <div>
          <h3>AI Optimization Insight</h3>

          <p>
            The planning engine identified that
            coordinating maintenance activities by
            corridor can reduce planned infrastructure
            downtime from{" "}
            <strong>
              {analytics.totalSeparateDowntime.toFixed(
                1
              )} hours
            </strong>{" "}
            to{" "}
            <strong>
              {analytics.totalOptimizedDowntime.toFixed(
                1
              )} hours
            </strong>
            , saving approximately{" "}
            <strong>
              {analytics.totalDowntimeSaved.toFixed(
                1
              )} hours
            </strong>{" "}
            of operational downtime.
          </p>
        </div>

      </div>

    </div>
  );
}

export default Analytics;