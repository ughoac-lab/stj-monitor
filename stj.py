"""Coleta decisões tributárias do STJ (via API do DJEN) e gera HTML.

Fonte: API pública do Diário de Justiça Eletrônico Nacional (DJEN/CNJ),
em comunicaapi.pje.jus.br. JSON limpo, dado fresco do dia.

Estratégia (igual ao robô do CARF):
- Busca "tributário" (âncora) na janela de datas e filtra LOCALMENTE os
  tributos de interesse (sem operador OU na API + limite de requisições).
- Janela móvel + memória (seen.json, por id) para nunca repetir nem perder.
- Página com filtro lateral por tributo (client-side).
"""

from __future__ import annotations

import base64
import datetime
import html
import json
import os
import re
import sys
import time
import webbrowser
from collections import Counter
from pathlib import Path

import requests

API_URL = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://comunica.pje.jus.br/",
    "Origin": "https://comunica.pje.jus.br",
}
ANCHOR = "tributário"
TRIBUNAL = "STJ"

OUTPUT_FILE = Path(__file__).parent / "index.html"
SEEN_FILE = Path(__file__).parent / "seen.json"
FULLTEXT_FILE = Path(__file__).parent / "decisoes-completas.txt"
TOKEN_FILE = Path(__file__).parent / "github_token.txt"
GITHUB_REPO = "ughoac-lab/stj-monitor"

QUERY_DAYS = 4           # busca: rede de segurança contra execução pulada/fim de semana
DISPLAY_DAYS = 1         # exibe hoje + ontem (cutoff = hoje - DISPLAY_DAYS)
SEEN_MAX_AGE_DAYS = 30   # tempo que um item fica na memória
PREVIEW_CHARS = 1400     # quanto do texto mostrar antes do link "ver completo"

WEEKDAY_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
MONTH_PT = ["", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
            "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]

# (rótulo, regex). Casamento por palavra inteira; "II" via expressão para
# não casar com "inciso II", "art. II" etc. ITCMD agrupa ITCMD e ITCM.
TERMS = [
    ("IRPJ", re.compile(r"\bIRPJ\b", re.I)),
    ("CSLL", re.compile(r"\bCSLL\b", re.I)),
    ("PIS", re.compile(r"\bPIS\b", re.I)),
    ("COFINS", re.compile(r"\bCOFINS\b", re.I)),
    ("ITBI", re.compile(r"\bITBI\b", re.I)),
    ("ISS", re.compile(r"\bISS\b|\bISSQN\b", re.I)),
    ("ICMS", re.compile(r"\bICMS\b", re.I)),
    ("Imposto de Renda", re.compile(r"imposto\s+de\s+renda", re.I)),
    ("CIDE", re.compile(r"\bCIDE\b", re.I)),
    ("IRRF", re.compile(r"\bIRRF\b", re.I)),
    ("IRPF", re.compile(r"\bIRPF\b", re.I)),
    ("IPI", re.compile(r"\bIPI\b", re.I)),
    ("Imposto de Importação",
     re.compile(r"imposto\s+(?:de|sobre\s+a)\s+importa[çc][ãa]o", re.I)),
    ("CBS", re.compile(r"\bCBS\b", re.I)),
    ("IBS", re.compile(r"\bIBS\b", re.I)),
    ("IVA", re.compile(r"\bIVA\b", re.I)),
    ("IOF", re.compile(r"\bIOF\b", re.I)),
    ("ITCMD", re.compile(r"\bITCMD\b|\bITCM\b", re.I)),
    ("IPTU", re.compile(r"\bIPTU\b", re.I)),
]


def _to_date(s: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def api_get(params: dict) -> dict:
    for attempt in range(6):
        r = requests.get(API_URL, params=params, headers=HEADERS, timeout=90)
        if r.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"  429 (limite), aguardando {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    return {"count": 0, "items": []}


def fetch_all(start: str, end: str) -> list[dict]:
    items: list[dict] = []
    pagina = 1
    while True:
        data = api_get({
            "siglaTribunal": TRIBUNAL, "meio": "D", "texto": ANCHOR,
            "dataDisponibilizacaoInicio": start, "dataDisponibilizacaoFim": end,
            "itensPorPagina": 100, "pagina": pagina,
        })
        total = data.get("count", 0)
        page = data.get("items", [])
        if not page:
            break
        items.extend(page)
        if len(items) >= total or pagina >= 120:
            break
        pagina += 1
        time.sleep(1.2)
    return items


def clean_text(raw: str | None) -> str:
    t = raw or ""
    t = re.sub(r"(?is)<(script|style).*?</\1>", "", t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|tr|div|h[1-6]|table)>", "\n", t)
    t = re.sub(r"(?i)</td>", " ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n[ \t]*", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def match_terms(text: str) -> list[str]:
    return [label for label, rx in TERMS if rx.search(text)]


# Marcadores onde começa o conteúdo substantivo (pula o cabeçalho do
# processo, que já é exibido nos metadados do card).
BODY_MARKERS = re.compile(
    r"(DECIS[ÃA]O|EMENTA|RELAT[ÓO]RIO|\bVOTO\b|Trata-se|Cuida-se|Vistos)", re.I)


def extract_body(text: str) -> str:
    m = BODY_MARKERS.search(text)
    return text[m.start():].strip() if m else text


def load_seen() -> dict[str, str]:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict[str, str], today: datetime.date) -> None:
    cutoff = today - datetime.timedelta(days=SEEN_MAX_AGE_DAYS)
    trimmed = {i: d for i, d in seen.items()
               if (_to_date(d) is None or _to_date(d) >= cutoff)}
    SEEN_FILE.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=0, sort_keys=True),
        encoding="utf-8",
    )


def fmt_date_long(d: datetime.date) -> str:
    return f"{WEEKDAY_PT[d.weekday()]}, {d.day} de {MONTH_PT[d.month]} de {d.year}"


CSS = """
    body { font-family: -apple-system, system-ui, Segoe UI, sans-serif;
           max-width: 1150px; margin: 2em auto; padding: 0 1em;
           color: #222; line-height: 1.5; }
    h1 { border-bottom: 2px solid #155; padding-bottom: 0.3em; margin-bottom: 0.2em; }
    .topo { font-size: 0.9em; margin: 0.3em 0 0.8em; }
    .topo a { color: #0a6b3b; }
    .status { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px;
              padding: 0.7em 1em; margin: 0.8em 0; font-size: 0.92em; color: #444; }
    .status div { margin: 0.15em 0; }
    .novidade { background: #eefcf3; border: 1px solid #b6e8cf;
                padding: 0.6em 1em; border-radius: 6px; margin: 0 0 1em 0; }
    .novidade.sem { background: #f5f5f5; border-color: #ddd; color: #777; }
    .novo { background: #1a7f4b; color: #fff; font-size: 0.7em;
            padding: 0.1em 0.5em; border-radius: 4px; font-weight: bold;
            vertical-align: middle; }
    .layout { display: flex; gap: 1.5em; align-items: flex-start; }
    .filtros { flex: 0 0 200px; position: sticky; top: 1em; font-size: 0.9em;
               max-height: calc(100vh - 2em); overflow-y: auto; }
    .conteudo { flex: 1; min-width: 0; }
    .filtros .grupo { margin-bottom: 1.3em; }
    .filtros h4 { margin: 0 0 0.4em; font-size: 0.95em; color: #333;
                  border-bottom: 1px solid #eee; padding-bottom: 0.2em; }
    .filtros ul { list-style: none; padding: 0; margin: 0; }
    .filtros li { padding: 0.3em 0.5em; border-radius: 4px; cursor: pointer;
                  color: #0a6b3b; }
    .filtros li:hover { background: #e7f6ee; }
    .filtros li.ativo { background: #1a7f4b; color: #fff; }
    .filtros li span { color: #999; font-size: 0.85em; }
    .filtros li.ativo span { color: #cfe9da; }
    .filtros li.todos { color: #666; font-style: italic; }
    h2.data { margin-top: 1.4em; font-size: 1.15em; color: #333;
              border-bottom: 1px solid #eee; padding-bottom: 0.2em; }
    h2.data .qtd { color: #999; font-weight: normal; font-size: 0.85em; }
    .empty { color: #999; font-style: italic; padding: 2em 0; text-align: center; }
    .acordao { border: 1px solid #ddd; border-radius: 6px;
               padding: 1em 1.2em; margin: 1em 0; background: #fafafa; }
    .acordao.novo-card { border-left: 4px solid #1a7f4b; background: #f6fdf9; }
    .acordao header { margin-bottom: 0.6em; }
    .acordao h3 { margin: 0 0 0.3em 0; font-size: 1.0em; }
    .acordao .meta { color: #555; font-size: 0.88em; }
    .acordao .tags { margin-top: 0.4em; }
    .tag { display: inline-block; background: #e7f0ff; color: #0a4ea3;
           font-size: 0.78em; padding: 0.05em 0.55em; border-radius: 10px;
           margin: 0.15em 0.2em 0.15em 0; }
    .acordao .ementa { white-space: pre-wrap; margin: 0.8em 0 0 0;
                       font-size: 0.92em; color: #333; }
    .acordao .mais { margin-top: 0.5em; font-size: 0.9em; }
    a { color: #0366d6; text-decoration: none; }
    a:hover { text-decoration: underline; }
    @media (max-width: 760px) {
      .layout { flex-direction: column; }
      .filtros { position: static; flex-basis: auto; }
    }
"""

JS = """
function aplicar(){
  var f = document.querySelector('.f-tributo.ativo');
  f = f ? f.getAttribute('data-val') : null;
  var cards = document.querySelectorAll('.acordao');
  for (var i=0;i<cards.length;i++){
    var c = cards[i];
    var t = (c.getAttribute('data-tributos')||'').split('|');
    c.style.display = (!f || t.indexOf(f) >= 0) ? '' : 'none';
  }
  var hs = document.querySelectorAll('h2.data');
  for (var j=0;j<hs.length;j++){
    var h = hs[j], el = h.nextElementSibling, vis = false;
    while (el && el.tagName !== 'H2'){
      if (el.className && (''+el.className).indexOf('acordao') >= 0 && el.style.display !== 'none') vis = true;
      el = el.nextElementSibling;
    }
    h.style.display = vis ? '' : 'none';
  }
}
function toggle(el, cls){
  var ativo = el.classList.contains('ativo');
  var todos = document.querySelectorAll('.'+cls);
  for (var i=0;i<todos.length;i++) todos[i].classList.remove('ativo');
  if (!ativo) el.classList.add('ativo');
  aplicar();
}
function limpar(cls){
  var todos = document.querySelectorAll('.'+cls);
  for (var i=0;i<todos.length;i++) todos[i].classList.remove('ativo');
  aplicar();
}
"""


def render_item(k: dict, is_new: bool) -> str:
    e = html.escape
    it = k["it"]
    proc = e(it.get("numeroprocessocommascara") or it.get("numero_processo") or "?")
    classe = e(it.get("nomeClasse") or "Decisão")
    orgao = e(it.get("nomeOrgao") or "")
    tipo = e(it.get("tipoDocumento") or it.get("tipoComunicacao") or "")
    link = it.get("link") or ""
    link_html = (f' · <a href="{e(link)}" target="_blank">documento</a>'
                 if link else "")
    dests = it.get("destinatarios") or []
    partes = "; ".join(d.get("nome", "") for d in dests[:6] if d.get("nome"))
    tags = " ".join(f'<span class="tag">{e(t)}</span>' for t in k["terms"])
    badge = '<span class="novo">NOVO</span> ' if is_new else ""
    cls = "acordao novo-card" if is_new else "acordao"
    data_t = e("|".join(k["terms"]))

    texto = extract_body(k["text"])
    preview = e(texto[:PREVIEW_CHARS])
    mais = ""
    if len(texto) > PREVIEW_CHARS and link:
        mais = (f'<div class="mais"><a href="{e(link)}" target="_blank">'
                f'[...] ver documento completo</a></div>')

    meta_extra = f" · {e(partes)}" if partes else ""
    return f"""<article class="{cls}" data-tributos="{data_t}">
  <header>
    <h3>{badge}{classe} — {proc}{link_html}</h3>
    <div class="meta">{orgao}<br>{tipo}{meta_extra}</div>
    <div class="tags">{tags}</div>
  </header>
  <div class="ementa">{preview}</div>
  {mais}
</article>"""


def _sidebar_group(titulo: str, counter: Counter, cls: str) -> str:
    e = html.escape
    itens = "".join(
        f'<li class="{cls}" data-val="{e(k)}" onclick="toggle(this,\'{cls}\')">'
        f'{e(k)} <span>({v})</span></li>'
        for k, v in counter.most_common()
    )
    return (f'<div class="grupo"><h4>{titulo}</h4><ul>'
            f'<li class="todos" onclick="limpar(\'{cls}\')">Todos</li>'
            f'{itens}</ul></div>')


def render_html(display: list[dict], new_ids: set, now: datetime.datetime,
                latest: datetime.date | None) -> str:
    term_counter: Counter = Counter()
    for k in display:
        for t in k["terms"]:
            term_counter[t] += 1

    groups: dict[datetime.date, list[dict]] = {}
    for k in display:
        groups.setdefault(k["date"], []).append(k)

    sections = []
    for d in sorted(groups, reverse=True):
        items = "\n".join(render_item(k, k["id"] in new_ids) for k in groups[d])
        sections.append(
            f'<h2 class="data">{fmt_date_long(d)} '
            f'<span class="qtd">({len(groups[d])})</span></h2>\n{items}'
        )
    body = ("\n".join(sections) if sections
            else '<p class="empty">Nenhuma decisão tributária na janela atual.</p>')

    n = len(new_ids)
    if n:
        aviso = (f'<p class="novidade">🔔 <b>{n}</b> decisão(ões) nova(s) nesta '
                 f'atualização (marcadas com <span class="novo">NOVO</span>).</p>')
    else:
        aviso = ('<p class="novidade sem">Nenhuma decisão nova desde a '
                 'última atualização.</p>')

    sidebar = _sidebar_group("Tributo", term_counter, "f-tributo") if display else ""

    now_str = now.strftime("%d/%m/%Y às %H:%M")
    latest_str = (f"{latest:%d/%m/%Y} ({WEEKDAY_PT[latest.weekday()]})"
                  if latest else "indisponível")
    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Decisões Tributárias — STJ</title>
  <style>{CSS}</style>
</head>
<body>
  <h1>Decisões Tributárias — STJ</h1>
  <div class="topo">→ <a href="https://ughoac-lab.github.io/stj-acordaos-mensal/">Ver acórdãos do mês (decisões colegiadas)</a></div>
  <div class="status">
    <div>🤖 Robô executou em <b>{now_str}</b> (se for hoje, está funcionando).</div>
    <div>📅 Publicação mais recente encontrada: <b>{latest_str}</b>.</div>
    <div>🪟 Mostra as decisões de hoje e ontem · fonte: DJEN · filtro "{ANCHOR}" + tributo.</div>
  </div>
  <div class="layout">
    <aside class="filtros">{sidebar}</aside>
    <main class="conteudo">
      {aviso}
      {body}
    </main>
  </div>
  <script>{JS}</script>
</body>
</html>"""


def write_fulltext(display: list[dict], now: datetime.datetime) -> None:
    """Salva o INTEIRO TEOR das decisoes exibidas num unico arquivo .txt
    (sobrescrito a cada execucao) para analise no chat do Claude."""
    out = [
        "DECISOES TRIBUTARIAS DO STJ - INTEIRO TEOR",
        f"Gerado em {now:%d/%m/%Y %H:%M} | {len(display)} decisoes (hoje + ontem)",
        "Fonte: DJEN. Suba este arquivo no chat do Claude e faca perguntas "
        "analiticas (ex: tese, se o contribuinte venceu ou perdeu).",
        "",
    ]
    for k in display:
        it = k["it"]
        proc = it.get("numeroprocessocommascara") or it.get("numero_processo") or "?"
        classe = it.get("nomeClasse") or "Decisao"
        orgao = it.get("nomeOrgao") or ""
        link = it.get("link") or ""
        dests = it.get("destinatarios") or []
        partes = "; ".join(x.get("nome", "") for x in dests if x.get("nome"))
        out.append("=" * 70)
        out.append(f"{k['date']:%d/%m/%Y} | {classe} | {proc}")
        out.append(f"Tributos: {', '.join(k['terms'])}")
        if orgao:
            out.append(f"Orgao: {orgao}")
        if partes:
            out.append(f"Partes: {partes}")
        if link:
            out.append(f"Link: {link}")
        out.append("-" * 70)
        out.append(k["text"])
        out.append("")
    FULLTEXT_FILE.write_text("\n".join(out), encoding="utf-8")


def publish_to_github(html_text: str) -> None:
    """Se existir github_token.txt, envia o index.html para o GitHub via API
    (sem precisar de Git). O GitHub Pages serve a pagina publicamente.
    Sem o arquivo de token (ex: rodando no PC de casa), nao publica."""
    if not TOKEN_FILE.exists():
        return
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        return
    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/index.html"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        r = requests.get(api, headers=headers, timeout=30)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {
            "message": f"Atualiza pagina STJ {datetime.date.today().isoformat()}",
            "content": base64.b64encode(html_text.encode("utf-8")).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        r = requests.put(api, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            print("Publicado no GitHub Pages com sucesso.")
        else:
            print(f"Falha ao publicar no GitHub: HTTP {r.status_code} - {r.text[:200]}")
    except Exception as e:
        print(f"Erro ao publicar no GitHub: {e}")


def main() -> None:
    today = datetime.date.today()
    if len(sys.argv) > 1:
        today = datetime.date.fromisoformat(sys.argv[1])
    start = today - datetime.timedelta(days=QUERY_DAYS)
    print(f"Janela: {start.isoformat()} a {today.isoformat()}")

    print(f"Buscando '{ANCHOR}' no DJEN ({TRIBUNAL})...")
    items = fetch_all(start.isoformat(), today.isoformat())
    print(f"Comunicações com '{ANCHOR}': {len(items)}")

    seen_in_batch: set = set()
    kept: list[dict] = []
    for it in items:
        i = it.get("id")
        if i in seen_in_batch:
            continue
        seen_in_batch.add(i)
        ct = clean_text(it.get("texto", ""))
        terms = match_terms(ct)
        if not terms:
            continue
        d = _to_date(it.get("data_disponibilizacao"))
        if d is None:
            continue
        kept.append({"it": it, "date": d, "terms": terms, "text": ct, "id": i})
    print(f"Após filtro de tributos: {len(kept)}")

    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    if first_run:
        new_ids: set = set()
        print("Primeira execução: estabelecendo memória (sem marcar NOVO).")
    else:
        new_ids = {k["id"] for k in kept if str(k["id"]) not in seen}
    print(f"Novos (não vistos antes): {len(new_ids)}")

    display_cutoff = today - datetime.timedelta(days=DISPLAY_DAYS)
    display = [k for k in kept if k["date"] >= display_cutoff or k["id"] in new_ids]
    display.sort(key=lambda k: k["it"].get("numeroprocessocommascara", "") or "")
    display.sort(key=lambda k: k["date"], reverse=True)

    latest = max((k["date"] for k in kept), default=None)
    now = datetime.datetime.now()
    html_text = render_html(display, new_ids, now, latest)
    OUTPUT_FILE.write_text(html_text, encoding="utf-8")
    print(f"HTML salvo: {OUTPUT_FILE} ({len(display)} decisões exibidas)")
    publish_to_github(html_text)

    write_fulltext(display, now)
    print(f"Inteiro teor salvo: {FULLTEXT_FILE}")

    for k in kept:
        seen[str(k["id"])] = k["date"].isoformat()
    save_seen(seen, today)
    print(f"Memória atualizada: {len(seen)} itens.")

    if not os.environ.get("CI"):
        webbrowser.open(OUTPUT_FILE.as_uri())
        print("Abrindo no navegador...")


if __name__ == "__main__":
    main()
