# Informe técnico — Proyecto Final

El informe incluye la carátula institucional de UTEC al inicio y las
figuras vectoriales generadas desde Python en `figuras/`.

## Regenerar las figuras

Desde la raíz del repositorio:

```bash
python informe/scripts/generar_figuras.py
```

El script usa `matplotlib` y escribe un PDF por figura en `informe/figuras/`
(la carátula ya no usa imágenes externas). Los datos de los experimentos
provienen de las mismas corridas reportadas en las tablas del informe.

## Compilar el PDF

Con `tectonic` (recomendado; descarga los paquetes LaTeX la primera vez):

```bash
cd informe
tectonic -X compile main.tex --outdir .
```

También se puede usar TeX Live en Docker (dos pasadas para el índice y los
enlaces; `:Z` es necesario en Fedora con SELinux):

```bash
docker run --rm -v "$PWD/informe:/work:Z" -w /work texlive/texlive:latest \
  pdflatex -interaction=nonstopmode main.tex
docker run --rm -v "$PWD/informe:/work:Z" -w /work texlive/texlive:latest \
  pdflatex -interaction=nonstopmode main.tex
```

Genera `main.pdf` (ignorado por git, al igual que los auxiliares de LaTeX).
