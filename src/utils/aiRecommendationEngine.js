const priorityWeight = {
  Critical: 100,
  High: 75,
  Medium: 50,
  Low: 25,
};

export function generateAIRecommendations(blockPlans) {
  return blockPlans
    .map((block) => {
      const criticalTasks = block.tasks.filter(
        (task) => task.priority === "Critical"
      ).length;

      const overdueTasks = block.tasks.filter(
        (task) => task.status === "Overdue"
      ).length;

      const highPriorityTasks = block.tasks.filter(
        (task) => task.priority === "High"
      ).length;

      const basePriority =
        priorityWeight[block.highestPriority];

      // Calculate urgency score
      let urgencyScore = basePriority;

      urgencyScore += criticalTasks * 5;
      urgencyScore += overdueTasks * 10;
      urgencyScore += highPriorityTasks * 2;

      // Coordination benefit
      const coordinationScore =
        block.departments.length * 8;

      // Downtime saving benefit
      const downtimeScore =
        block.downtimeSaved * 4;

      // Final AI score
      const aiScore = Math.min(
        Math.round(
          urgencyScore +
            coordinationScore +
            downtimeScore
        ),
        100
      );

      // Determine recommendation level
      let recommendationLevel = "Medium";

      if (aiScore >= 90) {
        recommendationLevel = "Critical";
      } else if (aiScore >= 70) {
        recommendationLevel = "High";
      } else if (aiScore >= 45) {
        recommendationLevel = "Medium";
      } else {
        recommendationLevel = "Low";
      }

      // Generate AI explanation
      const reasons = [];

      if (criticalTasks > 0) {
        reasons.push(
          `${criticalTasks} critical maintenance task${
            criticalTasks > 1 ? "s" : ""
          } require immediate attention`
        );
      }

      if (overdueTasks > 0) {
        reasons.push(
          `${overdueTasks} overdue maintenance task${
            overdueTasks > 1 ? "s" : ""
          } detected`
        );
      }

      if (block.departments.length >= 2) {
        reasons.push(
          `${block.departments.length} departments can be coordinated in one maintenance block`
        );
      }

      if (block.downtimeSaved > 0) {
        reasons.push(
          `approximately ${block.downtimeSaved.toFixed(
            1
          )} hours of infrastructure downtime can be saved`
        );
      }

      // Main recommendation explanation
      const explanation = `${block.corridor} is recommended for ${
        recommendationLevel === "Critical"
          ? "immediate"
          : "priority"
      } scheduling. The system detected ${
        criticalTasks > 0
          ? "critical maintenance requirements"
          : "important maintenance requirements"
      } and identified an opportunity to coordinate ${
        block.taskCount
      } maintenance activities across ${
        block.departments.length
      } department${
        block.departments.length > 1 ? "s" : ""
      } within a single optimized block.`;

      return {
        id: `REC-${block.id}`,
        blockId: block.id,
        corridor: block.corridor,
        recommendationLevel,
        aiScore,
        criticalTasks,
        overdueTasks,
        highPriorityTasks,
        coordinationScore,
        downtimeScore,
        reasons,
        explanation,
        downtimeSaved: block.downtimeSaved,
        departments: block.departments,
        blockDate: block.blockDate,
        timeSlot: block.timeSlot,
        aiConfidence: block.aiConfidence,
      };
    })
    .sort((a, b) => b.aiScore - a.aiScore);
}