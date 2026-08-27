// Tema claro/oscuro: la clase `dark` en <html> la aplica el script inline de
// index.html antes del primer paint; este hook la refleja en estado React,
// persiste el cambio en localStorage ("theme": "dark" | "light") y lo expone
// a los componentes que lo necesitan (p. ej. MapPanel para los tiles).
import { useCallback, useEffect, useState } from 'react'

function currentTheme() {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

export default function useTheme() {
  const [theme, setTheme] = useState(currentTheme)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
  }, [theme])

  const toggleTheme = useCallback(() => {
    setTheme((t) => {
      const next = t === 'dark' ? 'light' : 'dark'
      try {
        localStorage.setItem('theme', next)
      } catch {
        // localStorage no disponible (modo privado): solo cambia en sesión.
      }
      return next
    })
  }, [])

  return [theme, toggleTheme]
}
