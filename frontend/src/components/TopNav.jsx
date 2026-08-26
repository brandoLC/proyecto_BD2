// Barra superior: marca MiniDB + estado de conexión con el backend.
export default function TopNav({ health }) {
  // health: 'checking' | 'ok' | 'down'
  const dot =
    health === 'ok'
      ? 'bg-success'
      : health === 'down'
        ? 'bg-error'
        : 'bg-helper animate-pulse'
  const label =
    health === 'ok'
      ? 'Backend conectado'
      : health === 'down'
        ? 'Backend no disponible'
        : 'Conectando…'

  return (
    <header className="flex items-center justify-between border-b border-hairline bg-surface px-6 py-3">
      <div className="flex items-center gap-2.5">
        {/* glifo spark negro */}
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M12 2c.7 5.5 4.5 9.3 10 10-5.5.7-9.3 4.5-10 10-.7-5.5-4.5-9.3-10-10 5.5-.7 9.3-4.5 10-10z"
            fill="#0c0a09"
          />
        </svg>
        <span className="font-heading text-[17px] font-medium tracking-tight text-ink">
          MiniDB
        </span>
        <span className="hidden text-xs text-helper sm:inline">
          CS2042 · Bases de Datos II
        </span>
      </div>
      <div className="flex items-center gap-2" title={label}>
        <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
        <span className="text-xs text-body">{label}</span>
      </div>
    </header>
  )
}
