#!/usr/bin/env python3
"""
Build do dashboard de funil (Meta Ads).

Le duas planilhas Google publicas-por-link (SOMENTE LEITURA), cruza as vendas
com as queries de anuncios pelas UTMs e gera dist/data.json + dist/index.html.

Nao depende de nenhuma biblioteca externa: roda com a stdlib do Python 3.
Nunca escreve nada nas planilhas.
"""

import csv
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
SITE = os.path.join(ROOT, "site")

UA = "Mozilla/5.0 (compatible; funnel-dashboard-build/1.0)"
BR_TZ = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------- utilidades


def log(msg):
    print(f"[build] {msg}", flush=True)


def fetch(url, tries=4):
    """GET com retry exponencial. Retorna texto decodificado."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            return raw.decode("utf-8-sig", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last = e
            wait = 2 ** i
            log(f"  tentativa {i + 1}/{tries} falhou ({e}); nova tentativa em {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"nao consegui baixar {url}: {last}")


def sheet_csv(spreadsheet_id, sheet_name=None):
    """
    Baixa uma aba como CSV.

    Sem sheet_name -> primeira aba via /export (saida crua).
    Com sheet_name -> /gviz/tq, unico endpoint publico que aceita aba por nome.
    """
    base = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    if sheet_name:
        q = urllib.parse.urlencode({"tqx": "out:csv", "headers": "1", "sheet": sheet_name})
        url = f"{base}/gviz/tq?{q}"
    else:
        url = f"{base}/export?format=csv"
    txt = fetch(url)
    if txt.lstrip().startswith("<") or "google.visualization.Query.setResponse" in txt[:200]:
        raise RuntimeError(
            f"a planilha {spreadsheet_id} nao retornou CSV. "
            "Confirme que o compartilhamento esta em 'qualquer pessoa com o link' "
            f"e que a aba '{sheet_name}' existe."
        )
    return list(csv.DictReader(io.StringIO(txt)))


def norm(s):
    """Chave de join: sem espacos duplicados, sem espacos nas pontas, case-insensitive."""
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


_THOUSANDS_ONLY = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def num(v):
    """
    Numero tolerante a formato BR e US.
    "127,9" -> 127.9 | "1.234,56" -> 1234.56 | "1,234.56" -> 1234.56 | "1.234" -> 1234
    """
    s = str(v if v is not None else "").strip()
    if not s:
        return 0.0
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or s in {"-", ".", ","}:
        return 0.0
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # 1.234,56 (BR)
            s = s.replace(".", "").replace(",", ".")
        else:                                     # 1,234.56 (US)
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif _THOUSANDS_ONLY.match(s):                # 1.234 -> milhar, nao decimal
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


_GVIZ_DATE = re.compile(r"Date\((\d+),(\d+),(\d+)")


def to_date(v):
    """Normaliza varias formas de data para 'YYYY-MM-DD'. Retorna None se nao der."""
    s = str(v or "").strip()
    if not s:
        return None
    m = _GVIZ_DATE.match(s)                       # Date(2026,6,29) — mes 0-indexado
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)) + 1, int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    s = s.split("T")[0].strip()
    s = re.split(r"\s+", s)[0] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else s
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)   # dd/mm/aaaa
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2})$", s)  # dd/mm/aa
    if m:
        return f"20{int(m.group(3)):02d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def col(row, name):
    """Le uma coluna tolerando diferencas de espaco/caixa no cabecalho."""
    if name in row:
        return row[name]
    want = norm(name)
    for k, v in row.items():
        if norm(k) == want:
            return v
    return ""


# ---------------------------------------------------------------- extracao


def load_ads(cfg, warnings):
    c = cfg["ads"]["columns"]
    raw = sheet_csv(cfg["ads"]["spreadsheet_id"], cfg["ads"].get("sheet_name"))
    log(f"  planilha de anuncios: {len(raw)} linhas")

    missing = [k for k in ("date", "campaign", "adset", "ad") if not any(norm(h) == norm(c[k]) for h in (raw[0] if raw else {}))]
    if missing:
        raise RuntimeError(f"colunas ausentes na planilha de anuncios: {missing}. Ajuste config.json.")

    rows, skipped = [], 0
    for r in raw:
        d = to_date(col(r, c["date"]))
        if not d:
            skipped += 1
            continue
        rows.append({
            "d": d,
            "c": re.sub(r"\s+", " ", str(col(r, c["campaign"]) or "").strip()) or "(sem campanha)",
            "s": re.sub(r"\s+", " ", str(col(r, c["adset"]) or "").strip()) or "(sem conjunto)",
            "a": re.sub(r"\s+", " ", str(col(r, c["ad"]) or "").strip()) or "(sem anuncio)",
            "imp": num(col(r, c["impressions"])),
            "clk": num(col(r, c["clicks"])),
            "lpv": num(col(r, c["page_views"])),
            "ic": num(col(r, c["checkouts"])),
            "spend": num(col(r, c["spend"])),
        })
    if skipped:
        warnings.append(f"{skipped} linha(s) da planilha de anuncios ignoradas por data invalida.")
    return rows


def load_sales(cfg, warnings):
    c = cfg["sales"]["columns"]
    raw = sheet_csv(cfg["sales"]["spreadsheet_id"], cfg["sales"].get("sheet_name"))
    log(f"  planilha de vendas: {len(raw)} linhas")

    # Qualificacao: aceita 1 ou mais perguntas. Forma nova:
    #   "qualified": {"mode":"all"|"any", "conditions":[{"column":..,"yes_values":[..]}, ..]}
    # Forma antiga (1 pergunta): {"column":.., "yes_values":[..]} — ainda suportada.
    # Respostas de formulario vem com underscore no lugar de espaco ("sim,_está");
    # normaliza underscore->espaco para casar de forma robusta.
    qnorm = lambda s: norm(str(s or "").replace("_", " "))
    qcfg = cfg["sales"].get("qualified") or {}
    qmode = (qcfg.get("mode") or "all").lower()
    qconds = qcfg.get("conditions")
    if not qconds and qcfg.get("column"):
        qconds = [{"column": qcfg["column"], "yes_values": qcfg.get("yes_values") or []}]
    qcond_sets = [(cd["column"], {qnorm(x) for x in (cd.get("yes_values") or [])}) for cd in (qconds or [])]

    def is_qual(r):
        if not qcond_sets:
            return 0
        hits = [qnorm(col(r, cc)) in yv for cc, yv in qcond_sets]
        return 1 if (all(hits) if qmode == "all" else any(hits)) else 0

    bds = cfg["sales"].get("breakdowns") or []

    ok_status = {norm(s) for s in cfg["sales"].get("approved_status") or []}
    rows, skipped, refused = [], 0, 0
    for r in raw:
        if ok_status:
            st = norm(col(r, c["status"]))
            if st and st not in ok_status:
                refused += 1
                continue
        d = to_date(col(r, c["date"]))
        if not d:
            skipped += 1
            continue
        rows.append({
            "d": d,
            "v": num(col(r, c["value"])),
            "src": re.sub(r"\s+", " ", str(col(r, c.get("utm_source", "")) or "").strip()),
            "uc": re.sub(r"\s+", " ", str(col(r, c["utm_campaign"]) or "").strip()),
            "us": re.sub(r"\s+", " ", str(col(r, c["utm_adset"]) or "").strip()),
            "ua": re.sub(r"\s+", " ", str(col(r, c["utm_ad"]) or "").strip()),
            "q": is_qual(r),
            "bd": [re.sub(r"\s+", " ", str(col(r, b["column"]) or "").strip()) for b in bds],
        })
    if refused:
        warnings.append(f"{refused} venda(s) descartadas por status diferente de aprovado.")
    if skipped:
        warnings.append(f"{skipped} venda(s) ignoradas por data invalida.")
    return rows


# ---------------------------------------------------------------- atribuicao


def attribute(ads, sales, warnings):
    """
    Casa cada venda com a hierarquia do Meta pelas UTMs, do mais especifico ao
    menos: anuncio -> conjunto -> campanha -> nao atribuida.

    O match e feito nas chaves normalizadas (a planilha de vendas tem valores com
    espaco sobrando que quebram a igualdade crua), mas o nome gravado na venda e
    sempre o nome canonico vindo da planilha de anuncios, para que o agrupamento
    do front-end bata exatamente com as linhas de gasto.
    """
    by_ad, by_set, by_camp = {}, {}, {}
    for r in ads:
        by_ad.setdefault((norm(r["c"]), norm(r["s"]), norm(r["a"])), (r["c"], r["s"], r["a"]))
        by_set.setdefault((norm(r["c"]), norm(r["s"])), (r["c"], r["s"]))
        by_camp.setdefault(norm(r["c"]), r["c"])

    out = defaultdict(int)
    for s in sales:
        kc, ks, ka = norm(s["uc"]), norm(s["us"]), norm(s["ua"])
        if not kc and not ks and not ka:
            s["c"], s["s"], s["a"], s["m"] = "(sem UTM)", "(sem UTM)", "(sem UTM)", "none"
        elif (kc, ks, ka) in by_ad:
            s["c"], s["s"], s["a"] = by_ad[(kc, ks, ka)]
            s["m"] = "ad"
        elif (kc, ks) in by_set:
            s["c"], s["s"] = by_set[(kc, ks)]
            s["a"] = f"(anuncio fora do periodo) {s['ua']}" if s["ua"] else "(sem anuncio)"
            s["m"] = "adset"
        elif kc in by_camp:
            s["c"] = by_camp[kc]
            s["s"] = f"(conjunto nao encontrado) {s['us']}" if s["us"] else "(sem conjunto)"
            s["a"] = f"(anuncio nao encontrado) {s['ua']}" if s["ua"] else "(sem anuncio)"
            s["m"] = "campaign"
        else:
            s["c"] = f"(fora do Meta) {s['uc']}" if s["uc"] else "(sem UTM)"
            s["s"] = s["us"] or "(sem conjunto)"
            s["a"] = s["ua"] or "(sem anuncio)"
            s["m"] = "unmatched"
        out[s["m"]] += 1

    total = len(sales)
    matched = total - out["none"] - out["unmatched"]
    log(f"  atribuicao: {dict(out)} — {matched}/{total} vendas ligadas a alguma campanha")
    if out["none"]:
        warnings.append(
            f"{out['none']} venda(s) sem nenhuma UTM — contam na receita total, "
            "mas ficam no bucket 'Nao atribuido' e nao entram nas tabelas de otimizacao."
        )
    if out["unmatched"]:
        warnings.append(
            f"{out['unmatched']} venda(s) com UTM que nao existe na planilha de anuncios "
            "(campanha de outra conta, periodo fora da janela exportada ou UTM digitada errada)."
        )
    if out["adset"] or out["campaign"]:
        warnings.append(
            f"{out['adset'] + out['campaign']} venda(s) casaram so ate conjunto/campanha, "
            "nao ate o anuncio."
        )
    return {k: out[k] for k in ("ad", "adset", "campaign", "unmatched", "none")}


# ---------------------------------------------------------------- saida


def build_funnel(cfg, now):
    """Baixa, cruza e monta o data.json de UM funil."""
    warnings = []
    ads = load_ads(cfg, warnings)
    sales = load_sales(cfg, warnings)
    if not ads:
        raise RuntimeError(f"[{cfg.get('id','?')}] planilha de anuncios vazia; abortando.")

    match_counts = attribute(ads, sales, warnings)
    dates = [r["d"] for r in ads] + [r["d"] for r in sales]

    traffic_src = cfg["sales"].get("traffic_source", "meta-ads")
    traffic_sales = sum(1 for s in sales if norm(s.get("src", "")) == norm(traffic_src))
    if traffic_sales < len(sales):
        warnings.append(
            f"{len(sales) - traffic_sales} lead(s) fora do trafego (utm_source != '{traffic_src}') — "
            "organico/direto; entram so como referencia."
        )

    data = {
        "meta": {
            "id": cfg.get("id"),
            "menu_label": cfg.get("menu_label", cfg.get("title", "Funil")),
            "title": cfg.get("title", "Funil de Trafego"),
            "goal": (float(cfg["goal"]) if cfg.get("goal") not in (None, "") else None),
            "tabs": cfg.get("tabs", ["total", "traffic", "regions", "report"]),
            "platform": cfg.get("platform", "Meta Ads"),
            "currency": cfg.get("currency", "BRL"),
            "tax": float(cfg.get("tax_multiplier", 1.0)),
            "traffic_source": traffic_src,
            "generated_at": now.isoformat(timespec="seconds"),
            "generated_at_br": now.astimezone(BR_TZ).strftime("%d/%m/%Y %H:%M"),
            "date_min": min(dates),
            "date_max": max(dates),
            "sales_tab": cfg["sales"].get("sheet_name") or "(primeira aba)",
            "ads_url": f"https://docs.google.com/spreadsheets/d/{cfg['ads']['spreadsheet_id']}",
            "sales_url": f"https://docs.google.com/spreadsheets/d/{cfg['sales']['spreadsheet_id']}",
            "counts": {"ads_rows": len(ads), "sales_rows": len(sales),
                       "traffic_sales": traffic_sales,
                       "qualified": sum(s.get("q", 0) for s in sales),
                       "attribution": match_counts},
            "breakdowns": [{"key": b["key"], "label": b["label"]}
                           for b in (cfg["sales"].get("breakdowns") or [])],
            "warnings": warnings,
        },
        "ads": ads,
        "sales": [{"d": s["d"], "v": s["v"], "src": s.get("src", ""),
                   "c": s["c"], "s": s["s"], "a": s["a"], "m": s["m"],
                   "q": s.get("q", 0), "bd": s.get("bd", [])} for s in sales],
    }

    gross = sum(r["spend"] for r in ads)
    log(f"  [{cfg.get('id')}] {data['meta']['date_min']}..{data['meta']['date_max']} | "
        f"gasto x{data['meta']['tax']} R$ {gross * data['meta']['tax']:,.2f} | "
        f"leads {len(sales)} | qualif {data['meta']['counts']['qualified']} | atrib {match_counts}")
    for w in warnings:
        log(f"    ! {w}")
    return data


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    # Config nova = {"funnels":[...]}; config antiga (1 funil) tambem funciona.
    funnels = cfg["funnels"] if isinstance(cfg, dict) and "funnels" in cfg else [cfg]
    now = datetime.now(timezone.utc)
    log(f"baixando planilhas (somente leitura) — {len(funnels)} funil(is)...")

    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    manifest = []
    for fc in funnels:
        fid = fc.get("id") or "funnel"
        data = build_funnel(fc, now)
        fname = f"data-{fid}.json"
        with open(os.path.join(DIST, fname), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        manifest.append({"id": fid, "menu_label": data["meta"]["menu_label"],
                         "title": data["meta"]["title"], "file": fname})

    with open(os.path.join(DIST, "funnels.json"), "w", encoding="utf-8") as f:
        json.dump({"funnels": manifest,
                   "generated_at_br": now.astimezone(BR_TZ).strftime("%d/%m/%Y %H:%M")},
                  f, ensure_ascii=False)

    build_id = now.strftime("%Y%m%d%H%M%S")
    with open(os.path.join(SITE, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__BUILD_ID__", build_id)
    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    # Impede o Jekyll do GitHub Pages de reprocessar a pasta.
    open(os.path.join(DIST, ".nojekyll"), "w").close()

    log(f"OK — build {build_id} · funis: {', '.join(m['id'] for m in manifest)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                      # noqa: BLE001 — falha limpa no CI
        print(f"[build] ERRO: {e}", file=sys.stderr)
        sys.exit(1)
