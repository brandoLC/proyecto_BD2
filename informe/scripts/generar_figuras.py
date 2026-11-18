#!/usr/bin/env python3
"""Genera las figuras vectoriales (PDF) del informe de MiniDB.

Uso:
    python informe/scripts/generar_figuras.py

Las figuras se escriben en ``informe/figuras/`` y se incluyen en
``informe/main.tex`` con ``\\includegraphics``. Los datos de los
experimentos son los reportados en el informe (mismas corridas que
produjeron las tablas).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

# ----------------------------------------------------------------------
# Paleta y estilo institucional
# ----------------------------------------------------------------------
UTEC = "#003366"
PETROLEO = "#006978"
OXIDO = "#B2581A"
GRAFITO = "#555555"
ARENA = "#F0F4F8"
VERDE = "#2f7d4f"
COLORES = [UTEC, PETROLEO, OXIDO, GRAFITO]

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "figuras"
OUT.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["DejaVu Serif"],
    "mathtext.fontset": "dejavuserif",
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "axes.edgecolor": "#777777",
    "axes.linewidth": 0.7,
    "axes.grid": True,
    "grid.color": "#00000018",
    "grid.linewidth": 0.6,
    "legend.fontsize": 7.5,
    "legend.frameon": True,
    "legend.framealpha": 0.95,
    "legend.edgecolor": "#BBBBBB",
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "xtick.color": "#444444",
    "ytick.color": "#444444",
    "figure.dpi": 200,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.04,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
})


def guardar(fig, nombre: str) -> None:
    """Guarda la figura en PDF vectorial (la incluimos con graphicx)."""
    fig.savefig(OUT / f"{nombre}.pdf")
    plt.close(fig)
    print(f"  {nombre}.pdf")


def config_ejes(ax, xlabel: str | None = None, ylabel: str | None = None,
                title: str | None = None) -> None:
    if xlabel:
        ax.set_xlabel(xlabel)
    if ylabel:
        ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title, pad=6)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


# ----------------------------------------------------------------------
# Diagramas (arquitectura, índices y planificador)
# ----------------------------------------------------------------------
def caja(ax, x: float, y: float, w: float, h: float, titulo: str,
         detalle: str, color: str, relleno: str | None = None) -> None:
    relleno = relleno or "white"
    patch = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.025,rounding_size=0.10",
        facecolor=relleno, edgecolor=color, linewidth=1.0, zorder=2,
    )
    ax.add_patch(patch)
    ax.text(x + w / 2, y + h - 0.10, titulo, ha="center", va="top",
            fontsize=9.8, fontweight="bold", color=color, zorder=3)
    ax.text(x + w / 2, y + 0.28 * h, detalle, ha="center", va="center",
            fontsize=7.8, color="#222222", linespacing=1.2, zorder=3)


def flecha(ax, origen, destino, color: str = GRAFITO, estilo: str = "-",
           etiqueta: str | None = None, curva: float = 0.0,
           dx: float = 0.0, dy: float = 0.14) -> None:
    ax.add_patch(FancyArrowPatch(
        origen, destino, arrowstyle="-|>", mutation_scale=9,
        linewidth=0.9, color=color, linestyle=estilo,
        shrinkA=3, shrinkB=3, zorder=1,
        connectionstyle=f"arc3,rad={curva}",
    ))
    if etiqueta:
        ax.text((origen[0] + destino[0]) / 2 + dx,
                (origen[1] + destino[1]) / 2 + dy,
                etiqueta, ha="center", va="bottom", fontsize=6.8,
                color=color, fontstyle="italic")


def figura_arquitectura() -> None:
    fig, ax = plt.subplots(figsize=(9.2, 5.8))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 9.6)
    ax.axis("off")

    caja(ax, 0.4, 8.4, 11.2, 1.05, "Frontend React + Vite + Leaflet",
         "workbench SQL · visor de plan e I/O · mapa espacial · "
         "recomendador e-commerce", UTEC, "#EDF3F9")
    caja(ax, 1.5, 6.8, 9.0, 1.05, "API REST (FastAPI)",
         "/api/query · /api/tables · /api/recommend · /api/images", PETROLEO, "#EAF4F4")
    caja(ax, 1.5, 5.2, 9.0, 1.05, "Motor SQL: parser → catálogo → ejecutor",
         "AST · validación semántica · plan con tiempos · "
         "planificador híbrido · DiskCounter", UTEC, "#EDF3F9")

    caja(ax, 0.3, 3.0, 3.8, 1.55, "Índices",
         "B+ Tree · Hash extensible\nR-Tree · Invertido · IVF",
         PETROLEO, "#EAF4F4")
    caja(ax, 4.2, 3.0, 3.5, 1.55, "Módulo textual",
         "stopwords + stemming\nMATCH · TF-IDF coseno",
         PETROLEO, "#EAF4F4")
    caja(ax, 7.9, 3.0, 3.8, 1.55, "Aplicación de IA",
         "SIFT/BoVW · ResNet-18\nrecomendador e-commerce",
         OXIDO, "#FBF0E8")

    caja(ax, 1.5, 1.45, 9.0, 1.05, "Almacenamiento en páginas de 4 KB",
         "slotted pages · heap file · sequential file + overflow · "
         "serialización struct", GRAFITO, "#F2F2F2")
    caja(ax, 1.5, 0.05, 9.0, 1.05, "Archivos binarios en DATA_DIR",
         ".heap/.seq/.ovf · .btree/.hash/.rtree · "
         ".dic/.inv/.docs/.nrm · .cen/.lst", GRAFITO, "#FAFAFA")

    flecha(ax, (6.0, 8.4), (6.0, 7.85))
    flecha(ax, (6.0, 6.8), (6.0, 6.25))
    flecha(ax, (4.2, 5.2), (2.2, 4.55))
    flecha(ax, (6.0, 5.2), (6.0, 4.55))
    flecha(ax, (2.2, 3.0), (3.4, 2.50))
    flecha(ax, (6.0, 3.0), (6.0, 2.50))
    flecha(ax, (11.0, 4.55), (10.55, 5.20), OXIDO, "--",
           "SQL generado", dx=0.55, dy=0.02)
    flecha(ax, (6.0, 1.45), (6.0, 1.10))

    guardar(fig, "arquitectura")


def figura_invertido() -> None:
    fig, ax = plt.subplots(figsize=(8.4, 3.7))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6)
    ax.axis("off")

    caja(ax, 0.2, 4.5, 2.2, 1.0, "Documentos", "filas TEXT/VARCHAR",
         UTEC, "#EDF3F9")
    caja(ax, 2.8, 4.5, 2.7, 1.0, "Preprocesamiento",
         "minúsculas · sin acentos\nstopwords · stemming Snowball",
         PETROLEO, "#EAF4F4")
    caja(ax, 5.9, 4.5, 2.1, 1.0, "Delta en memoria",
         "posteos (doc_id, tf)", PETROLEO, "#EAF4F4")
    caja(ax, 8.4, 4.5, 3.3, 1.0, "flush()",
         "fusiona listas y recalcula normas", PETROLEO, "#EAF4F4")

    caja(ax, 8.4, 2.6, 3.3, 1.25, "Archivos del índice",
         ".dic vocabulario + df/offset\n.inv listas (doc_id, tf)\n"
         ".docs RID + lápida · .nrm normas", GRAFITO, "#F2F2F2")

    caja(ax, 0.2, 0.4, 2.4, 1.0, "Consulta MATCH",
         "'gato AND perro'", OXIDO, "#FBF0E8")
    caja(ax, 3.0, 0.4, 2.4, 1.0, "Parser booleano",
         "AND · OR · NOT · paréntesis", OXIDO, "#FBF0E8")
    caja(ax, 5.8, 0.4, 2.6, 1.0, "Recuperación",
         "merge de posteos (booleano)\no Top-K coseno TF-IDF", OXIDO, "#FBF0E8")
    caja(ax, 8.8, 0.4, 2.9, 1.0, "RIDs → heap",
         "fetch de filas vivas", UTEC, "#EDF3F9")

    flecha(ax, (2.4, 5.0), (2.8, 5.0))
    flecha(ax, (5.5, 5.0), (5.9, 5.0))
    flecha(ax, (8.0, 5.0), (8.4, 5.0))
    flecha(ax, (10.05, 4.5), (10.05, 3.85))
    flecha(ax, (2.6, 0.9), (3.0, 0.9))
    flecha(ax, (5.4, 0.9), (5.8, 0.9))
    flecha(ax, (8.4, 0.9), (8.8, 0.9))
    flecha(ax, (8.4, 3.2), (8.2, 1.40), GRAFITO, "--",
           "listas de posteo", dx=0.75, dy=-0.55)

    ax.text(1.0, 3.15,
            "consulta: una lectura por lista de posteo + fetch de RIDs",
            fontsize=7.2, color=GRAFITO, style="italic")
    guardar(fig, "invertido")


def figura_ivf() -> None:
    fig, ax = plt.subplots(figsize=(8.4, 3.9))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6.2)
    ax.axis("off")

    caja(ax, 0.2, 4.7, 2.3, 1.0, "Vectores", "columna VECTOR(n)\nfloat32",
         UTEC, "#EDF3F9")
    caja(ax, 2.9, 4.7, 2.6, 1.0, "K-Means (k-means++)",
         "K ≈ ⌊√N⌋ · 25 iteraciones", PETROLEO, "#EAF4F4")
    caja(ax, 5.9, 4.7, 2.4, 1.0, "Centroides", "archivo .cen\nD · K · métrica",
         PETROLEO, "#EAF4F4")
    caja(ax, 8.7, 4.7, 3.1, 1.0, "Asignación y listas",
         "cada vector a su celda\n.lst: páginas de 4 KB", PETROLEO, "#EAF4F4")

    caja(ax, 0.2, 0.4, 2.4, 1.0, "Consulta q", "embedding 512-D",
         OXIDO, "#FBF0E8")
    caja(ax, 3.0, 0.4, 2.8, 1.0, "Elegir probes",
         "celdas con centroide\nmás cercano", OXIDO, "#FBF0E8")
    caja(ax, 6.2, 0.4, 2.9, 1.0, "Escanear listas",
         "leer páginas de esas celdas", OXIDO, "#FBF0E8")
    caja(ax, 9.5, 0.4, 2.3, 1.0, "Top-K", "max-heap → RIDs",
         UTEC, "#EDF3F9")

    flecha(ax, (2.5, 5.2), (2.9, 5.2))
    flecha(ax, (5.5, 5.2), (5.9, 5.2))
    flecha(ax, (8.3, 5.2), (8.7, 5.2))
    flecha(ax, (2.6, 0.9), (3.0, 0.9))
    flecha(ax, (5.8, 0.9), (6.2, 0.9))
    flecha(ax, (9.1, 0.9), (9.5, 0.9))
    flecha(ax, (7.1, 4.7), (4.4, 1.40), GRAFITO, "--",
           "centroides", dx=-0.65, dy=0.20)
    flecha(ax, (10.2, 4.7), (8.0, 1.40), GRAFITO, "--",
           "páginas .lst", dx=0.65, dy=0.55)

    ax.text(1.0, 3.05,
            "I/O ∝ probes/K del archivo de listas, no a la tabla completa",
            fontsize=7.2, color=GRAFITO, style="italic")
    guardar(fig, "ivf")


def figura_planificador() -> None:
    fig, ax = plt.subplots(figsize=(8.2, 5.4))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 8)
    ax.axis("off")

    caja(ax, 3.4, 6.9, 5.2, 0.90, "SELECT ... embedding <=> q",
         "distancia vectorial en el SELECT", UTEC, "#EDF3F9")
    caja(ax, 3.2, 5.4, 5.6, 0.90, "¿WHERE con índice relacional?",
         "B+ Tree · Hash · R-Tree", PETROLEO, "#EAF4F4")
    caja(ax, 3.2, 3.9, 5.6, 0.90, "¿Hay índice IVF?",
         "búsqueda ANN por centroides", PETROLEO, "#EAF4F4")
    caja(ax, 3.2, 2.4, 5.6, 0.90, "¿WHERE vacío?",
         "solo se llega aquí si hay IVF", PETROLEO, "#EAF4F4")

    caja(ax, 9.1, 5.4, 2.7, 0.90, "Pre-filtering",
         "RIDs del índice →\ndistancia exacta", VERDE, "#EBF5EE")
    caja(ax, 0.2, 3.9, 2.6, 0.90, "Exact Vector Scan",
         "k-NN lineal\n(ground truth)", GRAFITO, "#F2F2F2")
    caja(ax, 9.1, 2.4, 2.7, 0.90, "ANN puro",
         "Top-K del IVF", VERDE, "#EBF5EE")
    caja(ax, 3.2, 0.75, 5.6, 0.90, "Post-filtering",
         "ANN primero → filtro WHERE\nsobre los candidatos", VERDE, "#EBF5EE")

    flecha(ax, (6.0, 6.9), (6.0, 6.3))
    flecha(ax, (8.8, 5.85), (9.1, 5.85), GRAFITO, "-", "sí")
    flecha(ax, (6.0, 5.4), (6.0, 4.8), GRAFITO, "-", "no",
           dx=0.28, dy=-0.28)
    flecha(ax, (3.2, 4.35), (2.8, 4.35), GRAFITO, "-", "no")
    flecha(ax, (6.0, 3.9), (6.0, 3.3), GRAFITO, "-", "sí",
           dx=0.28, dy=-0.28)
    flecha(ax, (8.8, 2.85), (9.1, 2.85), GRAFITO, "-", "sí")
    flecha(ax, (6.0, 2.4), (6.0, 1.65), GRAFITO, "-", "no",
           dx=0.28, dy=-0.28)

    ax.text(6.0, 0.28,
            "El paso «Vector Strategy» del plan reporta la decisión.",
            ha="center", fontsize=7.2, color=GRAFITO, style="italic")
    guardar(fig, "planificador")


# ----------------------------------------------------------------------
# Experimentos
# ----------------------------------------------------------------------
def figura_exp1() -> None:
    n = np.array([1_000, 10_000, 50_000, 100_000])
    series = {
        "Heap sin índice": [0.004, 0.045, 0.228, 0.476],
        "Heap + B+ Tree": [0.019, 0.234, 1.404, 3.590],
        "Heap + Hash ext.": [0.016, 0.195, 1.094, 2.545],
        "Sequential File": [0.006, 0.066, 0.334, 0.674],
    }
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 2.8),
                                   gridspec_kw={"width_ratios": [1.6, 1]})
    for (label, y), color in zip(series.items(), COLORES):
        ax1.plot(n, y, marker="o", markersize=3.6, linewidth=1.3,
                 color=color, label=label)
    ax1.set_xscale("log")
    ax1.set_yscale("log")
    ax1.set_xticks([1_000, 10_000, 50_000, 100_000])
    ax1.set_xticklabels(["1k", "10k", "50k", "100k"])
    config_ejes(ax1, "N (registros)", "Tiempo total (s)")
    ax1.legend(loc="upper left")

    orgs = ["Heap", "B+ Tree", "Hash", "Sequential"]
    writes = [1192, 1543, 1450, 1432]
    barras = ax2.bar(orgs, writes, color=COLORES, width=0.62)
    ax2.bar_label(barras, padding=2, fontsize=7)
    ax2.set_ylim(0, 1750)
    config_ejes(ax2, None, "Escrituras de bloque (N = 100k)")
    ax2.tick_params(axis="x", rotation=20)
    fig.tight_layout(w_pad=2.0)
    guardar(fig, "exp1_insercion")


def figura_exp2() -> None:
    labels = ["Heap\n(full scan)", "B+ Tree", "Hash ext.", "Sequential"]
    reads = [1192, 6.01, 5.0, 16.94]
    reads_std = [0.0, 0.09, 0.0, 0.26]
    lat = [183.5, 0.26, 0.20, 0.24]
    lat_std = [9.4, 0.03, 0.02, 0.02]
    y = np.arange(len(labels))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 2.4), sharey=True)
    ax1.errorbar(reads, y, xerr=reads_std, fmt="o", capsize=3,
                 color=UTEC, ecolor=UTEC, markersize=5)
    ax2.errorbar(lat, y, xerr=lat_std, fmt="s", capsize=3,
                 color=OXIDO, ecolor=OXIDO, markersize=5)
    ax1.set_xscale("log")
    ax2.set_xscale("log")
    ax1.set_yticks(y)
    ax1.set_yticklabels(labels)
    ax1.invert_yaxis()
    config_ejes(ax1, "Lecturas de bloque (prom.)", None)
    config_ejes(ax2, "Latencia (ms, prom.)", None)
    for vals, ax, unidad in ((reads, ax1, ""), (lat, ax2, "")):
        for yi, v in zip(y, vals):
            ax.annotate(f"{v:g}{unidad}", (v, yi), textcoords="offset points",
                        xytext=(0, 7), ha="center", fontsize=7, color="#333333")
    ax1.set_xlim(3.5, 2000)
    ax2.set_xlim(0.15, 300)
    fig.tight_layout(w_pad=2.0)
    guardar(fig, "exp2_busquedas")


def figura_exp3() -> None:
    sel = np.array([0.1, 1, 5, 10, 25])
    scan = [1192, 1192, 1192, 1192, 1192]
    btree = [101.56, 686.28, 1196.64, 1228.52, 1278.0]
    seq = [18.24, 51.42, 204.22, 396.02, 972.22]

    fig, ax = plt.subplots(figsize=(6.8, 3.0))
    ax.plot(sel, scan, marker="o", markersize=4, color=GRAFITO,
            linewidth=1.4, label="Heap sin índice (full scan)")
    ax.plot(sel, btree, marker="s", markersize=4, color=UTEC,
            linewidth=1.4, label="Heap + B+ Tree")
    ax.plot(sel, seq, marker="^", markersize=4.5, color=PETROLEO,
            linewidth=1.4, label="Sequential File")
    ax.axhline(1192, color=GRAFITO, linestyle=":", linewidth=0.9)
    ax.set_xscale("log")
    ax.set_xticks([0.1, 1, 5, 10, 25])
    ax.set_xticklabels(["0.1", "1", "5", "10", "25"])
    ax.set_ylim(0, 1400)
    config_ejes(ax, "Selectividad del rango (% del total)",
                "Lecturas de bloque (prom.)")
    ax.legend(loc="upper left")
    ax.annotate("cruce 1–5 %", xy=(3.0, 1100), xytext=(1.2, 1250),
                fontsize=7.5, color=OXIDO,
                arrowprops=dict(arrowstyle="->", color=OXIDO, lw=0.8))
    fig.tight_layout()
    guardar(fig, "exp3_rangos")


def figura_exp4() -> None:
    b = np.array([1024, 2048, 4096, 8192])
    interno = [72, 145, 292, 584]
    hoja = [101, 203, 408, 818]
    altura = [3, 3, 3, 2]
    lecturas = [3.03, 3.01, 3.01, 2.00]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 2.9))
    ax1.plot(b, interno, marker="o", markersize=4, color=UTEC,
             label="Nodo interno")
    ax1.plot(b, hoja, marker="s", markersize=4, color=PETROLEO,
             label="Nodo hoja")
    ax1.set_xscale("log", base=2)
    ax1.set_yscale("log", base=2)
    ax1.set_xticks(b)
    ax1.set_xticklabels(["1K", "2K", "4K", "8K"])
    config_ejes(ax1, "Tamaño de bloque B (bytes)",
                "Capacidad (entradas/nodo)")
    ax1.legend(loc="upper left")

    ax2b = ax2.twinx()
    barras = ax2.bar([str(x) for x in b], altura, color=UTEC, width=0.5,
                     alpha=0.80, label="Altura h")
    ax2.bar_label(barras, padding=2, fontsize=7)
    ax2.set_ylim(0, 3.6)
    config_ejes(ax2, "Tamaño de bloque B (bytes)", "Altura h")
    ax2b.plot([str(x) for x in b], lecturas, marker="o", markersize=4,
              color=OXIDO, linewidth=1.3, label="Lecturas prom.")
    ax2b.set_ylim(1.8, 3.3)
    ax2b.set_ylabel("Lecturas prom. por búsqueda", color=OXIDO)
    ax2b.tick_params(axis="y", colors=OXIDO)
    ax2b.spines["right"].set_visible(True)
    ax2b.spines["top"].set_visible(False)
    lines = [barras, ax2b.lines[0]]
    ax2.legend(lines, ["Altura h", "Lecturas prom."], loc="lower left")
    fig.tight_layout(w_pad=1.8)
    guardar(fig, "exp4_bloque")


def figura_exp5a() -> None:
    n = np.array([10_000, 50_000, 100_000])
    series = {
        "Scan (P páginas)": ([160, 795, 1589], [20.2, 98.9, 201.1], GRAFITO, "--"),
        "R-Tree radio 0.005°": ([15.3, 43.1, 70.4], [0.47, 1.25, 2.38], UTEC, "-"),
        "R-Tree radio 0.05°": ([159.4, 747.7, 1476.4], [4.00, 32.7, 85.2], OXIDO, "-"),
        "KNN k=5": ([17.3, 31.0, 41.0], [0.51, 2.11, 4.46], PETROLEO, "-"),
        "KNN k=50": ([59.1, 81.4, 94.2], [1.25, 2.49, 4.62], VERDE, "-"),
    }
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 2.9))
    for label, (reads, lat, color, ls) in series.items():
        ax1.plot(n, reads, marker="o", markersize=3.5, linewidth=1.2,
                 color=color, linestyle=ls, label=label)
        ax2.plot(n, lat, marker="o", markersize=3.5, linewidth=1.2,
                 color=color, linestyle=ls, label=label)
    for ax in (ax1, ax2):
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks([10_000, 50_000, 100_000])
        ax.set_xticklabels(["10k", "50k", "100k"])
        config_ejes(ax, "N (puntos)", None)
    ax1.set_ylabel("Lecturas de bloque (prom.)")
    ax2.set_ylabel("Latencia (ms, prom.)")
    ax1.legend(loc="upper left", fontsize=6.8)
    fig.tight_layout(w_pad=2.0)
    guardar(fig, "exp5a_espacial")


def figura_exp5b() -> None:
    d = np.array([2, 4, 8, 16, 32, 64, 128])
    frac = [2.6, 20.31, 62.42, 98.04, 100, 100, 100]
    hojas = [10.4, 81.3, 249.7, 392.2, 400, 400, 400]

    fig, ax = plt.subplots(figsize=(6.8, 3.0))
    ax.plot(d, frac, marker="o", markersize=4, color=OXIDO, linewidth=1.5)
    ax.axhline(100, color=GRAFITO, linestyle=":", linewidth=0.9)
    ax.axvline(32, color=GRAFITO, linestyle="--", linewidth=0.8)
    ax.set_xscale("log", base=2)
    ax.set_xticks(d)
    ax.set_xticklabels([str(x) for x in d])
    ax.set_ylim(0, 107)
    config_ejes(ax, "Dimensión D", "Hojas visitadas (%)")
    for xi, yi, hi in zip(d, frac, hojas):
        ax.annotate(f"{hi:g}", (xi, yi), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=6.8,
                    color="#444444")
    ax.text(2.2, 103, "número sobre el punto: hojas visitadas de 400",
            fontsize=6.8, color=GRAFITO, style="italic")
    fig.tight_layout()
    guardar(fig, "exp5b_dim")


def figura_exp6a() -> None:
    n = np.array([1_000, 2_000, 5_000, 10_000, 20_000])
    minidb = [201.1, 432.0, 1_070.6, 2_172.2, 3_890.0]
    gin = [89.3, 141.7, 311.1, 610.7, 1_143.8]
    tam_kb = [379.4, 643.1, 1_417.0, 2_703.9, 5_276.4]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 2.9))
    ax1.plot(n, minidb, marker="o", markersize=4, color=UTEC,
             label="MiniDB (INVERTED)")
    ax1.plot(n, gin, marker="s", markersize=4, color=OXIDO,
             label="PostgreSQL (GIN)")
    ax1.set_xscale("log")
    ax1.set_yscale("log")
    ax1.set_xticks(n)
    ax1.set_xticklabels(["1k", "2k", "5k", "10k", "20k"])
    config_ejes(ax1, "N (documentos)", "Tiempo de construcción (ms)")
    ax1.legend(loc="upper left")

    ax2.plot(n, tam_kb, marker="^", markersize=4.5, color=PETROLEO,
             linewidth=1.4)
    ax2.set_xscale("log")
    ax2.set_xticks(n)
    ax2.set_xticklabels(["1k", "2k", "5k", "10k", "20k"])
    config_ejes(ax2, "N (documentos)", "Tamaño del índice (KB)")
    ax2.annotate("≈264 B/documento", xy=(20_000, 5_276.4),
                 xytext=(4_000, 4_500), fontsize=7,
                 color=GRAFITO,
                 arrowprops=dict(arrowstyle="->", color=GRAFITO, lw=0.8))
    fig.tight_layout(w_pad=2.0)
    guardar(fig, "exp6a_construccion")


def figura_exp6b() -> None:
    n = np.array([1_000, 2_000, 5_000, 10_000, 20_000])
    booleano = {
        "Full Text Scan": [236.0, 484.4, 1_231.0, 2_471.4, 4_918.6],
        "Índice invertido": [2.08, 2.77, 8.16, 13.68, 24.73],
        "GIN (PostgreSQL)": [0.05, 0.09, 0.12, 0.22, 0.25],
    }
    topk = {
        "Full Text Scan": [183.5, 362.2, 1_012.3, 1_913.6, 3_545.9],
        "Índice invertido": [1.54, 2.04, 7.45, 12.29, 26.55],
        "GIN (PostgreSQL)": [0.04, 0.05, 0.04, 0.06, 0.06],
    }
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 2.9))
    for (label, y), color, marker in zip(booleano.items(), COLORES,
                                         ("o", "s", "^")):
        ax1.plot(n, y, marker=marker, markersize=3.8, linewidth=1.3,
                 color=color, label=label)
    for (label, y), color, marker in zip(topk.items(), COLORES,
                                         ("o", "s", "^")):
        ax2.plot(n, y, marker=marker, markersize=3.8, linewidth=1.3,
                 color=color, label=label)
    for ax, titulo in ((ax1, "Consulta booleana (3 términos)"),
                       (ax2, "Top-10 con COSINE_SCORE()")):
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks(n)
        ax.set_xticklabels(["1k", "2k", "5k", "10k", "20k"])
        config_ejes(ax, "N (documentos)", "Latencia (ms)", titulo)
    ax1.legend(loc="upper left")
    fig.tight_layout(w_pad=2.0)
    guardar(fig, "exp6b_latencia")


def figura_exp7() -> None:
    labels = ["BoVW +\ninvertido", "Embedding +\nIVF"]
    p5 = [0.417, 0.963]
    p10 = [0.358, 0.952]
    extraccion = [18.0, 80.8]
    consulta = [12.2, 5.6]
    bloques = [81.0, 100.4]

    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(7.8, 2.8))
    x = np.arange(len(labels))
    ancho = 0.34
    b1 = ax1.bar(x - ancho / 2, p5, ancho, color=UTEC, label="P@5")
    b2 = ax1.bar(x + ancho / 2, p10, ancho, color=PETROLEO, label="P@10")
    ax1.bar_label(b1, fmt="%.3f", padding=2, fontsize=6.8)
    ax1.bar_label(b2, fmt="%.3f", padding=2, fontsize=6.8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.set_ylim(0, 1.25)
    config_ejes(ax1, None, "Precisión", "Efectividad")
    ax1.legend(loc="upper left", ncol=2, fontsize=7)

    ax2.bar(x - ancho / 2, extraccion, ancho, color=OXIDO,
            label="Extracción")
    ax2.bar(x + ancho / 2, consulta, ancho, color=GRAFITO,
            label="Consulta")
    for xi, v in zip(x - ancho / 2, extraccion):
        ax2.annotate(f"{v:g}", (xi, v), textcoords="offset points",
                     xytext=(0, 2), ha="center", fontsize=6.8)
    for xi, v in zip(x + ancho / 2, consulta):
        ax2.annotate(f"{v:g}", (xi, v), textcoords="offset points",
                     xytext=(0, 2), ha="center", fontsize=6.8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels)
    ax2.set_ylim(0, 95)
    config_ejes(ax2, None, "Tiempo (ms)", "Costo")
    ax2.legend(loc="upper right")

    b3 = ax3.bar(x, bloques, 0.5, color=(PETROLEO, OXIDO))
    ax3.bar_label(b3, fmt="%.1f", padding=2, fontsize=6.8)
    ax3.set_xticks(x)
    ax3.set_xticklabels(labels)
    ax3.set_ylim(0, 120)
    config_ejes(ax3, None, "Bloques leídos", "I/O de consulta")
    fig.tight_layout(w_pad=1.8)
    guardar(fig, "exp7_bovw")


def figura_exp8() -> None:
    probes = [1, 2, 5, 10, 20, 50]
    recall = [0.757, 0.920, 0.980, 1.000, 1.000, 1.000]
    latencia = [1.47, 1.97, 3.40, 9.21, 12.09, 17.15]
    bloques = [88.9, 114.1, 186.5, 300.5, 553.9, 660.0]
    x = np.arange(len(probes))

    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(7.6, 2.8))
    ax1.plot(x, recall, marker="o", markersize=4, color=UTEC,
             linewidth=1.4)
    ax1.set_ylim(0.70, 1.04)
    ax1.set_xticks(x)
    ax1.set_xticklabels([str(p) for p in probes])
    config_ejes(ax1, "probes", "Recall@10")
    ax1.axhline(1.0, color=GRAFITO, linestyle=":", linewidth=0.8)

    ax2.plot(x, latencia, marker="s", markersize=4, color=OXIDO,
             linewidth=1.4)
    ax2.axhline(37.0, color=GRAFITO, linestyle=":", linewidth=0.9)
    ax2.text(0.1, 37.8, "k-NN exacto: 37.0 ms", fontsize=7,
             color=GRAFITO, style="italic")
    ax2.set_xticks(x)
    ax2.set_xticklabels([str(p) for p in probes])
    config_ejes(ax2, "probes", "Latencia (ms)")
    ax2.set_ylim(0, 41)

    ax3.plot(x, bloques, marker="^", markersize=4.5, color=PETROLEO,
             linewidth=1.4)
    ax3.set_xticks(x)
    ax3.set_xticklabels([str(p) for p in probes])
    config_ejes(ax3, "probes", "Bloques leídos")
    fig.tight_layout(w_pad=1.8)
    guardar(fig, "exp8_ann")


def main() -> None:
    print(f"Generando figuras en {OUT} ...")
    figura_arquitectura()
    figura_invertido()
    figura_ivf()
    figura_planificador()
    figura_exp1()
    figura_exp2()
    figura_exp3()
    figura_exp4()
    figura_exp5a()
    figura_exp5b()
    figura_exp6a()
    figura_exp6b()
    figura_exp7()
    figura_exp8()
    print("Listo.")


if __name__ == "__main__":
    main()
