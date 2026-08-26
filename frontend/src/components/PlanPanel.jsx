// Pestaña Plan: pasos de ejecución con tiempos.
export default function PlanPanel({ result }) {
  const plan = result?.plan

  if (!plan || plan.length === 0) {
    return (
      <p className="py-10 text-center text-xs text-helper">
        Ejecuta una consulta para ver su plan de ejecución.
      </p>
    )
  }

  const total = result?.elapsed_ms

  return (
    <div>
      <ol className="flex flex-col">
        {plan.map((step, i) => (
          <li
            key={step.step ?? i}
            className="flex items-start gap-3 border-b border-hairline py-3 last:border-b-0"
          >
            <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-accent font-mono text-[11px] text-accent-edge">
              {step.step ?? i + 1}
            </span>
            <div className="min-w-0 flex-1">
              <p className="font-heading text-sm font-medium text-ink">{step.name}</p>
              {step.detail && (
                <p className="mt-0.5 break-words font-mono text-xs text-helper">
                  {step.detail}
                </p>
              )}
            </div>
            <span className="shrink-0 font-mono text-xs text-body">
              {step.time_ms != null ? `${step.time_ms} ms` : ''}
            </span>
          </li>
        ))}
      </ol>
      {total != null && (
        <p className="mt-4 border-t border-hairline pt-3 text-right font-mono text-xs text-body">
          Tiempo total: <span className="text-ink">{total} ms</span>
        </p>
      )}
    </div>
  )
}
