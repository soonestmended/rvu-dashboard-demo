// Wraps a dollar figure so the demo can show the compensation machinery (columns, levers,
// projections) while blurring the actual numbers. `on` = redact mode (the public demo stack).
// The value is fabricated demo data regardless; the blur keeps salary-looking figures off the
// screen at a live/public demo. Non-dollar values (RVUs, %, counts) are never wrapped.
export default function Redacted({ on, children }) {
  if (!on) return <>{children}</>;
  // Blur in `em` so it scales with font size — a big headline dollar figure gets proportionally
  // more blur than a small table cell, so nothing stays legible (a fixed 5px left large text
  // readable). 0.45em ≈ 14px on a 2xl headline, ≈ 5px on body text.
  return (
    <span
      style={{
        filter: 'blur(0.45em)',
        userSelect: 'none',
        WebkitUserSelect: 'none',
        display: 'inline-block',
      }}
      title="Redacted for the demo"
      aria-hidden="true"
    >
      {children}
    </span>
  );
}
