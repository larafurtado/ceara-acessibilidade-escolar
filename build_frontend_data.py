#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_frontend_data.py
======================
Gera os dois arquivos de dados que a interface (FNDE Dashboard.dc.html) consome:

  1) ceara_data.js          -> window.CEARA = {viewBox, munis:[...]}  (geometria + agregados + esc)
  2) municipios_detalhe.json -> {municipio:{fase:{cenario:{percentil:{...}}}}}

IMPORTANTE (para o Claude Code):
- Escreva os arquivos DIRETO no disco com este script (open().write()).
  NÃO passe o conteúdo por comando de shell (echo/cat/heredoc) — estoura o limite do shell.
- Rode:  python build_frontend_data.py
- Dependências: apenas a biblioteca padrão para (1). Para ler parquet/o detalhe,
  use pandas/pyarrow (já presentes no projeto Streamlit).

Convenções que a interface EXIGE (não alterar sem avisar o front):
- Proporções são frações 0..1 (ex.: 0.04 = 4%).
- Etapas: 'creche' -> chave "D" (Daycare) ; 'pre_escola' -> chave "P" (Pre-school).
- Nas chaves de `fase` do JSON de detalhe use "Daycare" / "Pre-school".
- O percentil NÃO afeta o mapa/agregados; só a tabela de thresholds no detalhe.
"""

import json
import math
import os

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ANALISES = os.path.dirname(_HERE)   # Análises Lara FNDE/

GEOJSON_PATH    = os.path.join(_HERE, "handoff", "ceara_municipios.geojson")
ESCOLAS_PARQUET = os.path.join(_ANALISES, "14 de maio 2026", "Outputs2", "base_quadrado_escola.parquet")
_OUTPUTS        = os.path.join(_ANALISES, "25 de maio 2026", "outputs")
ORIG_PARQUET    = os.path.join(_OUTPUTS, "analise_1_creche", "parquets", "resultado_municipio_cenario_fase.parquet")
CAL_PARQUET     = os.path.join(_OUTPUTS, "analise_1_creche_calibrada_tempo", "parquets", "resultado_municipio_cenario_fase_calibrado_tempo.parquet")

OUT_DIR = os.path.join(_HERE, "handoff_frontend", "handoff_frontend")
OUT_JS  = os.path.join(OUT_DIR, "ceara_data.js")
OUT_DET = os.path.join(OUT_DIR, "municipios_detalhe.json")

# bounding box do Ceará (mantém o viewBox idêntico ao do protótipo: [0,0,1000,1222])
MINX, MAXX = -41.424, -37.253
MINY, MAXY = -7.858, -2.784
CANVAS_W   = 1000
DP_EPS     = 0.7     # tolerância Douglas-Peucker em unidades de TELA (já projetadas)

# ----------------------------------------------------------------------------
# PROJEÇÃO (equiretangular) + SIMPLIFICAÇÃO (Douglas-Peucker) -> path SVG
# ----------------------------------------------------------------------------
_lon_span = MAXX - MINX
_lat_span = MAXY - MINY
_cos_lat  = math.cos(math.radians((MINY + MAXY) / 2.0))
_k   = CANVAS_W / _lon_span
_ky  = _k / _cos_lat
CANVAS_H = round(_lat_span * _ky)
VIEWBOX  = [0, 0, CANVAS_W, CANVAS_H]

def project(lon, lat):
    return ((lon - MINX) * _k, (MAXY - lat) * _ky)

def _dp(points, eps):
    """Douglas-Peucker sobre lista de (x,y) já projetados."""
    if len(points) < 4:
        return points
    sq = eps * eps
    def perp_sq(p, a, b):
        x, y = a; dx, dy = b[0]-x, b[1]-y
        if dx or dy:
            t = ((p[0]-x)*dx + (p[1]-y)*dy) / (dx*dx + dy*dy)
            if t > 1: x, y = b
            elif t > 0: x, y = x+dx*t, y+dy*t
        return (p[0]-x)**2 + (p[1]-y)**2
    keep = [False]*len(points); keep[0] = keep[-1] = True
    stack = [(0, len(points)-1)]
    while stack:
        s, e = stack.pop()
        dmax, idx = 0.0, -1
        for i in range(s+1, e):
            d = perp_sq(points[i], points[s], points[e])
            if d > dmax: dmax, idx = d, i
        if dmax > sq:
            keep[idx] = True
            stack.append((s, idx)); stack.append((idx, e))
    return [p for p, k in zip(points, keep) if k]

def _ring_to_path(ring):
    proj = [project(lon, lat) for lon, lat in ring]
    proj = _dp(proj, DP_EPS)
    if len(proj) < 3:
        return ""
    return "M" + "L".join("%.1f %.1f" % (x, y) for x, y in proj) + "Z"

def geom_to_path(geom):
    t = geom["type"]; coords = geom["coordinates"]; d = ""
    if t == "Polygon":
        for ring in coords:
            d += _ring_to_path(ring)
    elif t == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                d += _ring_to_path(ring)
    return d

def r5(v):
    return None if v is None else round(float(v), 5)

# ----------------------------------------------------------------------------
# (1) ceara_data.js
# ----------------------------------------------------------------------------
def load_esc_by_muni():
    """
    Retorna { NM_MUN: {"D": {escolas,matriculas,docentes}, "P": {...}} }.
    Fonte: base_quadrado_escola.parquet (colunas separadas por fase, não coluna 'etapa').
    """
    if not os.path.exists(ESCOLAS_PARQUET):
        print("  [esc] parquet ausente -> seguindo sem 'esc' (front usa fallback)")
        return {}
    import pandas as pd
    cols = [
        "Código INEP", "NM_MUN_escola",
        "_TP_SITUACAO_FUNCIONAMENTO", "_TP_DEPENDENCIA",
        "_QT_MAT_INF_CRE", "_QT_MAT_INF_PRE",
        "_QT_DOC_INF_CRE", "_QT_DOC_INF_PRE",
    ]
    df = pd.read_parquet(ESCOLAS_PARQUET, columns=cols)
    # apenas escolas ativas e públicas; uma linha por escola
    df = df[
        (df["_TP_SITUACAO_FUNCIONAMENTO"] == 1) &
        (df["_TP_DEPENDENCIA"].isin([1, 2, 3]))
    ].drop_duplicates(subset="Código INEP")

    out = {}
    for muni, grp in df.groupby("NM_MUN_escola"):
        d_mask = grp["_QT_MAT_INF_CRE"] > 0
        p_mask = grp["_QT_MAT_INF_PRE"] > 0
        out[muni] = {
            "D": {
                "escolas":    int(d_mask.sum()),
                "matriculas": int(grp.loc[d_mask, "_QT_MAT_INF_CRE"].sum()),
                "docentes":   int(grp.loc[d_mask, "_QT_DOC_INF_CRE"].sum()),
            },
            "P": {
                "escolas":    int(p_mask.sum()),
                "matriculas": int(grp.loc[p_mask, "_QT_MAT_INF_PRE"].sum()),
                "docentes":   int(grp.loc[p_mask, "_QT_DOC_INF_PRE"].sum()),
            },
        }
    print("  [esc] municípios com Censo: %d" % len(out))
    return out

def build_ceara_data():
    with open(GEOJSON_PATH, encoding="utf-8") as f:
        gj = json.load(f)
    esc_by_muni = load_esc_by_muni()

    munis = []
    for feat in gj["features"]:
        p = feat["properties"]; nm = p["NM_MUN"]
        rec = {
            "n": nm,
            "d": geom_to_path(feat["geometry"]),
            "pop": {
                "D": int(round(p.get("pop_alvo_creche") or 0)),
                "P": int(round(p.get("pop_alvo_pre_escola") or 0)),
            },
            "so": {  # sem acesso - ORIGINAL (distância)
                "D": {c: r5(p.get("prop_sem_acesso_orig_creche_%s" % c))     for c in ("C1","C2","C3")},
                "P": {c: r5(p.get("prop_sem_acesso_orig_pre_escola_%s" % c)) for c in ("C1","C2","C3")},
            },
            "sc": {  # sem acesso - CALIBRADO (tempo)
                "D": {c: r5(p.get("prop_sem_acesso_cal_creche_%s" % c))     for c in ("C1","C2","C3")},
                "P": {c: r5(p.get("prop_sem_acesso_cal_pre_escola_%s" % c)) for c in ("C1","C2","C3")},
            },
            "dl": {
                "D": r5(p.get("delta_sem_acesso_creche")),
                "P": r5(p.get("delta_sem_acesso_pre_escola")),
            },
        }
        if nm in esc_by_muni:
            rec["esc"] = esc_by_muni[nm]
        munis.append(rec)

    payload = {"viewBox": VIEWBOX, "munis": munis}
    js = "window.CEARA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";"
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write(js)
    print("OK %s  (%d municípios, viewBox %s, %.0f KB)" %
          (OUT_JS, len(munis), VIEWBOX, len(js)/1024))

# ----------------------------------------------------------------------------
# (2) municipios_detalhe.json
# ----------------------------------------------------------------------------
def build_detalhe():
    """
    Constrói {municipio:{"Daycare"/"Pre-school":{cenario:{percentil:{...}}}}}.
    Fonte: resultado_municipio_cenario_fase.parquet (original) +
           resultado_municipio_cenario_fase_calibrado_tempo.parquet (calibrado).
    """
    import pandas as pd

    THRESHOLDS = {
        ("Daycare",    "Urbana"): {"p75": 2.69,  "p90": 3.80,  "p95": 4.62},
        ("Daycare",    "Rural"):  {"p75": 9.98,  "p90": 16.37, "p95": 21.19},
        ("Pre-school", "Urbana"): {"p75": 2.44,  "p90": 3.50,  "p95": 4.28},
        ("Pre-school", "Rural"):  {"p75": 9.79,  "p90": 15.97, "p95": 20.63},
    }
    FASE_MAP = {"creche": "Daycare", "pre_escola": "Pre-school"}

    df_orig = pd.read_parquet(ORIG_PARQUET)
    df_cal  = pd.read_parquet(CAL_PARQUET)
    orig_idx = df_orig.set_index(["NM_MUN", "fase", "cenario"])
    cal_idx  = df_cal.set_index(["NM_MUN", "fase", "cenario"])

    out = {}
    for mun in sorted(df_orig["NM_MUN"].unique()):
        for fase_code, fase_label in FASE_MAP.items():
            for cenario in ["C1", "C2", "C3"]:
                try:
                    o = orig_idx.loc[(mun, fase_code, cenario)]
                    c = cal_idx.loc[(mun, fase_code, cenario)]
                except KeyError:
                    continue
                delta = float(c["prop_sem_acesso"]) - float(o["prop_sem_acesso"])
                for percentil in ["p75", "p90", "p95"]:
                    node = (out.setdefault(mun, {})
                               .setdefault(fase_label, {})
                               .setdefault(cenario, {}))
                    node[percentil] = {
                        "municipio": mun,
                        "fase": fase_label,
                        "cenario": cenario,
                        "percentil": percentil,
                        "pop_alvo": round(float(o["pop_alvo"]), 2),
                        "original": {
                            "caminhavel":    round(float(o["prop_caminhavel"]), 6),
                            "so_transporte": round(float(o["prop_so_transporte"]), 6),
                            "sem_acesso":    round(float(o["prop_sem_acesso"]), 6),
                        },
                        "calibrado": {
                            "caminhavel":    round(float(c["prop_caminhavel"]), 6),
                            "so_transporte": round(float(c["prop_so_transporte"]), 6),
                            "sem_acesso":    round(float(c["prop_sem_acesso"]), 6),
                        },
                        "delta_sem_acesso": round(delta, 6),
                        "thresholds": [
                            {"fase": fase_label, "situacao": sit,
                             percentil: THRESHOLDS[(fase_label, sit)][percentil]}
                            for sit in ("Urbana", "Rural")
                        ],
                    }

    with open(OUT_DET, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("OK %s  (%d municípios)" % (OUT_DET, len(out)))

# ----------------------------------------------------------------------------
if __name__ == "__main__":
    print("Gerando dados do front...")
    build_ceara_data()
    build_detalhe()
    print("Pronto. Copie os arquivos gerados para a pasta servida (handoff_frontend/).")
