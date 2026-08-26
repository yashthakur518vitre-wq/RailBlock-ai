function StatCard({
  title,
  value,
  subtitle,
  icon: Icon,
  trend,
  trendType = "positive",
}) {
  return (
    <div className="stat-card">
      <div className="stat-card-top">
        <div className={`stat-icon ${trendType}`}>
          <Icon size={22} />
        </div>

        {trend && (
          <span className={`trend ${trendType}`}>
            {trend}
          </span>
        )}
      </div>

      <div className="stat-content">
        <p>{title}</p>
        <h2>{value}</h2>
        <span>{subtitle}</span>
      </div>
    </div>
  );
}

export default StatCard;