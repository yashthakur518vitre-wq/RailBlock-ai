import { useState } from "react";
import {
  Search,
  Filter,
  AlertTriangle,
  Clock,
  CheckCircle2,
  ClipboardList,
} from "lucide-react";

import { maintenanceTasks } from "../data/maintenanceData";

function Tasks() {
  const [searchTerm, setSearchTerm] = useState("");
  const [departmentFilter, setDepartmentFilter] = useState("All");
  const [priorityFilter, setPriorityFilter] = useState("All");

  const filteredTasks = maintenanceTasks.filter((task) => {
    const matchesSearch =
      task.task.toLowerCase().includes(searchTerm.toLowerCase()) ||
      task.corridor.toLowerCase().includes(searchTerm.toLowerCase()) ||
      task.id.toLowerCase().includes(searchTerm.toLowerCase());

    const matchesDepartment =
      departmentFilter === "All" ||
      task.department === departmentFilter;

    const matchesPriority =
      priorityFilter === "All" ||
      task.priority === priorityFilter;

    return (
      matchesSearch &&
      matchesDepartment &&
      matchesPriority
    );
  });

  const criticalTasks = maintenanceTasks.filter(
    (task) => task.priority === "Critical"
  ).length;

  const overdueTasks = maintenanceTasks.filter(
    (task) => task.status === "Overdue"
  ).length;

  const scheduledTasks = maintenanceTasks.filter(
    (task) => task.status === "Scheduled"
  ).length;

  return (
    <div className="tasks-page">

      <div className="page-heading">
        <div>
          <p className="welcome-label">CENTRALIZED MAINTENANCE SYSTEM</p>
          <h1>Maintenance Tasks</h1>
          <p>
            View and manage maintenance data from Engineering,
            Signal & Telecommunication, and Traction departments.
          </p>
        </div>
      </div>

      <div className="task-summary-grid">

        <div className="task-summary-card">
          <div className="summary-icon blue">
            <ClipboardList size={22} />
          </div>
          <div>
            <span>Total Tasks</span>
            <h2>{maintenanceTasks.length}</h2>
          </div>
        </div>

        <div className="task-summary-card">
          <div className="summary-icon red">
            <AlertTriangle size={22} />
          </div>
          <div>
            <span>Critical Tasks</span>
            <h2>{criticalTasks}</h2>
          </div>
        </div>

        <div className="task-summary-card">
          <div className="summary-icon orange">
            <Clock size={22} />
          </div>
          <div>
            <span>Overdue</span>
            <h2>{overdueTasks}</h2>
          </div>
        </div>

        <div className="task-summary-card">
          <div className="summary-icon green">
            <CheckCircle2 size={22} />
          </div>
          <div>
            <span>Scheduled</span>
            <h2>{scheduledTasks}</h2>
          </div>
        </div>

      </div>

      <div className="task-table-container">

        <div className="task-table-header">
          <div>
            <h2>All Maintenance Tasks</h2>
            <p>{filteredTasks.length} tasks found</p>
          </div>

          <div className="task-filters">

            <div className="task-search">
              <Search size={18} />
              <input
                type="text"
                placeholder="Search tasks or corridors..."
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
              />
            </div>

            <select
              value={departmentFilter}
              onChange={(e) =>
                setDepartmentFilter(e.target.value)
              }
            >
              <option value="All">All Departments</option>
              <option value="Engineering">Engineering</option>
              <option value="Signal & Telecommunication">
                Signal & Telecommunication
              </option>
              <option value="Traction Distribution">
                Traction Distribution
              </option>
            </select>

            <select
              value={priorityFilter}
              onChange={(e) =>
                setPriorityFilter(e.target.value)
              }
            >
              <option value="All">All Priorities</option>
              <option value="Critical">Critical</option>
              <option value="High">High</option>
              <option value="Medium">Medium</option>
              <option value="Low">Low</option>
            </select>

          </div>
        </div>

        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Task ID</th>
                <th>Maintenance Task</th>
                <th>Department</th>
                <th>Corridor</th>
                <th>Priority</th>
                <th>Status</th>
                <th>Due Date</th>
                <th>Duration</th>
              </tr>
            </thead>

            <tbody>
              {filteredTasks.map((task) => (
                <tr key={task.id}>
                  <td className="task-id">{task.id}</td>

                  <td>
                    <strong>{task.task}</strong>
                    <span className="task-system">
                      {task.system} • {task.location}
                    </span>
                  </td>

                  <td>{task.department}</td>

                  <td>{task.corridor}</td>

                  <td>
                    <span
                      className={`priority ${task.priority.toLowerCase()}`}
                    >
                      {task.priority}
                    </span>
                  </td>

                  <td>
                    <span
                      className={`status ${task.status.toLowerCase()}`}
                    >
                      {task.status}
                    </span>
                  </td>

                  <td>{task.dueDate}</td>

                  <td>{task.estimatedHours} hrs</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

      </div>
    </div>
  );
}

export default Tasks;