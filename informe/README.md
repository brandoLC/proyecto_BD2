# Informe técnico — Entregable 1

Compilar (2 pasadas para el índice; `:Z` necesario en Fedora con SELinux):

```bash
docker run --rm -v "$PWD/informe:/work:Z" -w /work texlive/texlive:latest \
  pdflatex -interaction=nonstopmode main.tex
docker run --rm -v "$PWD/informe:/work:Z" -w /work texlive/texlive:latest \
  pdflatex -interaction=nonstopmode main.tex
```

Genera `main.pdf` (ignorado por git, al igual que los auxiliares de LaTeX).
