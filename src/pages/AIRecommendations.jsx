import { useMemo, useState } from "react";

import {
  Sparkles,
  BrainCircuit,
  AlertTriangle,
  Clock,
  Users,
  TrendingDown,
  ChevronDown,
  ChevronUp,
  CheckCircle2,
} from "lucide-react";

import { maintenanceTasks } from "../data/maintenanceData";
import { generateBlockPlan } from "../utils/blockPlanner";
import { generateAIRecommendations } from "../utils/aiRecommendationEngine";

function AIRecommendations() {
  const [expandedRecommendation, setExpandedRecommendation] =
    useState(null);

  const recommendations = useMemo(() => {
    const blockPlans =
      generateBlockPlan(maintenanceTasks);

    return generateAIRecommendations(blockPlans);
  }, []);

  const criticalCount = recommendations.filter(
    (item) =>
      item.recommendationLevel === "Critical"
  ).length;

  const highCount = recommendations.filter(
    (item) =>
      item.recommendationLevel === "High"
  ).length;

  const totalDowntimeSaved = recommendations.reduce(
    (total, item) =>
      total + item.downtimeSaved,
    0
  );

  const getRecommendationIcon = (level) => {
    if (level === "Critical") {
      return <AlertTriangle size={21} />;
    }

    if (level === "High") {
      return <TrendingDown size={21} />;
    }

    return <BrainCircuit size={21} />;
  };

  return (
    <div className="ai-page">

      {/* PAGE HEADER */}

      <div className="page-heading ai-heading">

        <div>
          <p className="welcome-label">
            EXPLAINABLE AI DECISION ENGINE
          </p>

          <h1>AI Recommendations</h1>

          <p>
            Understand why the system prioritizes
            specific maintenance blocks using task
            urgency, criticality, coordination
            opportunities, and downtime impact.
          </p>
        </div>

        <div className="ai-status">
          <Sparkles size={17} />
          AI ANALYSIS COMPLETE
        </div>

      </div>

      {/* SUMMARY */}

      <div className="ai-summary-grid">

        <div className="ai-summary-card">
          <div className="summary-icon red">
            <AlertTriangle size={21} />
          </div>

          <div>
            <span>Critical Recommendations</span>
            <h2>{criticalCount}</h2>
          </div>
        </div>

        <div className="ai-summary-card">
          <div className="summary-icon orange">
            <BrainCircuit size={21} />
          </div>

          <div>
            <span>High Priority</span>
            <h2>{highCount}</h2>
          </div>
        </div>

        <div className="ai-summary-card">
          <div className="summary-icon green">
            <TrendingDown size={21} />
          </div>

          <div>
            <span>Potential Downtime Saved</span>
            <h2>
              {totalDowntimeSaved.toFixed(1)} hrs
            </h2>
          </div>
        </div>

        <div className="ai-summary-card">
          <div className="summary-icon purple">
            <BrainCircuit size={21} />
          </div>

          <div>
            <span>AI Recommendations</span>
            <h2>{recommendations.length}</h2>
          </div>
        </div>

      </div>

      {/* RECOMMENDATION LIST */}

      <section className="recommendation-section">

        <div className="section-title">
          <div>
            <h2>Priority Recommendations</h2>

            <p>
              AI-generated explanations for optimized
              maintenance scheduling decisions.
            </p>
          </div>

          <div className="recommendation-ready">
            <CheckCircle2 size={16} />
            EXPLAINABLE RESULTS
          </div>
        </div>

        <div className="recommendation-list">

          {recommendations.map((recommendation) => {
            const isExpanded =
              expandedRecommendation ===
              recommendation.id;

            return (
              <div
                className="recommendation-card"
                key={recommendation.id}
              >

                <div className="recommendation-main">

                  <div
                    className={`recommendation-icon ${recommendation.recommendationLevel.toLowerCase()}`}
                  >
                    {getRecommendationIcon(
                      recommendation.recommendationLevel
                    )}
                  </div>

                  <div className="recommendation-content">

                    <div className="recommendation-top">

                      <div>
                        <span className="block-reference">
                          {recommendation.blockId}
                        </span>

                        <h3>
                          {recommendation.corridor}
                        </h3>
                      </div>

                      <span
                        className={`priority ${recommendation.recommendationLevel.toLowerCase()}`}
                      >
                        {
                          recommendation.recommendationLevel
                        }{" "}
                        Priority
                      </span>

                    </div>

                    <p>
                      {recommendation.explanation}
                    </p>

                    <div className="recommendation-meta">

                      <span>
                        <Clock size={14} />
                        {
                          recommendation.blockDate
                        }
                      </span>

                      <span>
                        <Users size={14} />
                        {
                          recommendation.departments
                            .length
                        }{" "}
                        Departments
                      </span>

                      <span>
                        <TrendingDown size={14} />
                        {
                          recommendation.downtimeSaved.toFixed(
                            1
                          )
                        }{" "}
                        hrs saved
                      </span>

                    </div>

                  </div>

                  <div className="ai-score">

                    <span>AI Score</span>

                    <strong>
                      {recommendation.aiScore}
                    </strong>

                    <small>/100</small>

                  </div>

                  <button
                    className="expand-button"
                    onClick={() =>
                      setExpandedRecommendation(
                        isExpanded
                          ? null
                          : recommendation.id
                      )
                    }
                  >
                    {isExpanded ? (
                      <ChevronUp size={20} />
                    ) : (
                      <ChevronDown size={20} />
                    )}
                  </button>

                </div>

                {isExpanded && (

                  <div className="recommendation-details">

                    <div className="reason-section">

                      <h4>
                        Why did the AI prioritize this?
                      </h4>

                      <div className="reason-list">

                        {recommendation.reasons.map(
                          (reason, index) => (
                            <div
                              className="reason-item"
                              key={index}
                            >
                              <CheckCircle2
                                size={16}
                              />

                              <span>
                                {reason}
                              </span>
                            </div>
                          )
                        )}

                      </div>

                    </div>

                    <div className="score-breakdown">

                      <h4>
                        AI Score Breakdown
                      </h4>

                      <div className="score-row">
                        <span>
                          Criticality & Urgency
                        </span>

                        <strong>
                          {
                            recommendation.aiScore -
                            recommendation.coordinationScore -
                            recommendation.downtimeScore
                          }
                        </strong>
                      </div>

                      <div className="score-row">
                        <span>
                          Multi-Department Coordination
                        </span>

                        <strong>
                          {
                            recommendation.coordinationScore
                          }
                        </strong>
                      </div>

                      <div className="score-row">
                        <span>
                          Downtime Optimization
                        </span>

                        <strong>
                          {
                            recommendation.downtimeScore.toFixed(
                              1
                            )
                          }
                        </strong>
                      </div>

                    </div>

                  </div>
                )}

              </div>
            );
          })}

        </div>

      </section>

    </div>
  );
}

export default AIRecommendations;