const rules = [
  ["material_amount_approval", "threshold", "require_approval"],
  ["1099_nec_trigger", "event", "notify"],
  ["foreign_payment_flag", "event", "tag"]
];

export default function SpecialtyRulesPage() {
  return (
    <section className="mx-auto max-w-6xl px-4 py-6">
      <h1 className="text-2xl font-semibold">Specialty Rules</h1>
      <div className="mt-4 grid gap-3">
        {rules.map(([kind, trigger, action]) => (
          <article key={kind} className="rounded-md border border-line bg-paper p-4">
            <h2 className="text-base font-semibold">{kind}</h2>
            <p className="mt-1 text-sm text-ink/70">{trigger} trigger, {action} action</p>
          </article>
        ))}
      </div>
    </section>
  );
}
