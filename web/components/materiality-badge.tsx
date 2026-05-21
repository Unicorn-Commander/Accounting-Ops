const tone = {
  low: "border-signal text-signal",
  review: "border-warn text-warn",
  block: "border-red-700 text-red-700"
};

export function MaterialityBadge({ level }: { level: keyof typeof tone }) {
  return (
    <span className={`inline-flex items-center rounded-md border px-2 py-1 text-xs font-semibold ${tone[level]}`}>
      {level === "low" ? "Auto-post" : level === "review" ? "Review" : "Human attestation"}
    </span>
  );
}
