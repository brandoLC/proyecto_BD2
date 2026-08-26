// Tarjeta de error de la API (ok:false) con badge de etapa.
const STAGE_LABEL = {
  parse: 'parse',
  semantic: 'semantic',
  execution: 'execution',
}

export default function StatusMessage({ error, stage }) {
  if (!error) return null
  return (
    <div className="rounded-card border border-error/50 bg-surface p-4 shadow-card">
      <div className="flex items-start gap-3">
        {stage && (
          <span className="mt-0.5 shrink-0 rounded-full border border-error/60 px-2 py-0.5 font-mono text-[10px] text-error">
            {STAGE_LABEL[stage] || stage}
          </span>
        )}
        <p className="font-mono text-[13px] leading-relaxed text-error">{error}</p>
      </div>
    </div>
  )
}
