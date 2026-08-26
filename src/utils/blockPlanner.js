export function generateBlockPlan(tasks) {
  // Only consider tasks that still require maintenance
  const activeTasks = tasks.filter(
    (task) =>
      task.status === "Pending" ||
      task.status === "Scheduled" ||
      task.status === "Overdue"
  );

  // Group tasks according to their corridor
  const corridorGroups = {};

  activeTasks.forEach((task) => {
    if (!corridorGroups[task.corridor]) {
      corridorGroups[task.corridor] = [];
    }

    corridorGroups[task.corridor].push(task);
  });

  const priorityWeight = {
    Critical: 4,
    High: 3,
    Medium: 2,
    Low: 1,
  };

  const blockPlans = Object.entries(corridorGroups).map(
    ([corridor, corridorTasks], index) => {
      // Sort most important tasks first
      const sortedTasks = [...corridorTasks].sort(
        (a, b) =>
          priorityWeight[b.priority] -
          priorityWeight[a.priority]
      );

      // Different departments involved
      const departments = [
        ...new Set(
          corridorTasks.map((task) => task.department)
        ),
      ];

      // If each task had a separate block
      const separateBlockHours = corridorTasks.reduce(
        (total, task) =>
          total + task.estimatedHours,
        0
      );

      // Coordinated block duration is based on
      // the longest maintenance activity
      const coordinatedDuration = Math.max(
        ...corridorTasks.map(
          (task) => task.estimatedHours
        )
      );

      // Add coordination buffer
      const coordinationBuffer =
        corridorTasks.length > 1 ? 0.5 : 0;

      const blockDuration =
        coordinatedDuration + coordinationBuffer;

      const downtimeSaved =
        separateBlockHours - blockDuration;

      // Determine highest priority
      const highestPriority =
        sortedTasks[0].priority;

      // Simple AI confidence calculation
      let aiConfidence = 70;

      if (departments.length >= 3) {
        aiConfidence += 15;
      } else if (departments.length === 2) {
        aiConfidence += 10;
      }

      if (
        highestPriority === "Critical" ||
        highestPriority === "High"
      ) {
        aiConfidence += 5;
      }

      aiConfidence = Math.min(
        aiConfidence,
        98
      );

      // Demo dates and time windows
      const blockDates = [
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
      ];

      const timeSlots = [
        "01:00 - 04:00",
        "02:00 - 05:00",
        "00:30 - 03:30",
        "01:30 - 04:30",
      ];

      return {
        id: `BLOCK-${String(index + 1).padStart(
          3,
          "0"
        )}`,
        corridor,
        tasks: sortedTasks,
        departments,
        highestPriority,
        blockDate: blockDates[index % blockDates.length],
        timeSlot: timeSlots[index % timeSlots.length],
        separateBlockHours,
        blockDuration,
        downtimeSaved,
        aiConfidence,
        taskCount: corridorTasks.length,
      };
    }
  );

  // Critical and high-priority corridors first
  return blockPlans.sort((a, b) => {
    return (
      priorityWeight[b.highestPriority] -
      priorityWeight[a.highestPriority]
    );
  });
}