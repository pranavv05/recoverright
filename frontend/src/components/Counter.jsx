export default function Counter({ label, value, tone }) {
  return (
    <div className={`counter ${tone || ''}`}>
      <div className="counter-value">{value}</div>
      <div className="counter-label">{label}</div>
    </div>
  )
}
