import {
  Clock,
  MapPin,
  ArrowRight,
  CheckCircle2,
} from "lucide-react";

function BlockCard({
  corridor,
  time,
  departments,
  priority,
  impact,
}) {
  return (
    <div className="block-card">
      <div className="block-card-header">
        <div>
          <span className="block-label">RECOMMENDED BLOCK</span>
          <h3>{corridor}</h3>
        </div>

        <span className="priority-badge">
          Priority {priority}
        </span>
      </div>

      <div className="block-details">
        <div>
          <Clock size={17} />
          <span>{time}</span>
        </div>

        <div>
          <MapPin size={17} />
          <span>{impact} Train Impact</span>
        </div>
      </div>

      <div className="departments">
        {departments.map((department) => (
          <span key={department}>
            <CheckCircle2 size={15} />
            {department}
          </span>
        ))}
      </div>

      <button className="view-block-button">
        View Block Details
        <ArrowRight size={17} />
      </button>
    </div>
  );
}

export default BlockCard;