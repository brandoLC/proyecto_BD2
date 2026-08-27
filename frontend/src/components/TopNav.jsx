// Barra superior: marca MiniDB + estado de conexión con el backend.
export default function TopNav({ health, theme, onToggleTheme }) {
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
        {/* glifo spark */}
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
          className="text-ink"
        >
          <path
            d="M12 2c.7 5.5 4.5 9.3 10 10-5.5.7-9.3 4.5-10 10-.7-5.5-4.5-9.3-10-10 5.5-.7 9.3-4.5 10-10z"
            fill="currentColor"
          />
        </svg>
        <span className="font-heading text-[17px] font-medium tracking-tight text-ink">
          MiniDB
        </span>
        <span className="hidden text-xs text-helper sm:inline">
          CS2042 · Bases de Datos II
        </span>
      </div>
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2" title={label}>
          <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
          <span className="text-xs text-body">{label}</span>
        </div>
        <button
          type="button"
          onClick={onToggleTheme}
          title={theme === 'dark' ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro'}
          aria-label={theme === 'dark' ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro'}
          className="rounded-full border border-hairline p-1.5 text-body transition-colors hover:bg-canvas hover:text-ink"
        >
          {theme === 'dark' ? (
            // sol: visible en modo oscuro
            <svg
              width="15"
              height="15"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
            </svg>
          ) : (
            // luna: visible en modo claro
            <svg
              width="15"
              height="15"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
            </svg>
          )}
        </button>
      </div>
    </header>
  )
}
