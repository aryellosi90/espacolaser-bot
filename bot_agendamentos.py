"""
Bot de Agendamentos - Espaço Laser
====================================
Fluxo:
  1. Login no EVUP
  2. Relatórios → Agendamentos
  3. Define Data Programada: hoje → hoje+14
  4. Clica em Buscar e aguarda carregar
  5. Clica em EXCEL → faz download
  6. Encontra o arquivo mais recente na pasta Downloads
  7. Lê o Excel com pandas e gera dois resumos:
       a) Agendamentos criados hoje (por loja e usuário)
       b) Agendamentos dos próximos dias (por tipo e loja por data)
  8. Envia as mensagens para o webhook N8N
"""

from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta
import pytz
import requests
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import base64
import glob
import time
import re
import os

# ===============================
# CONFIGURAÇÕES
# ===============================
TZ_SP = pytz.timezone("America/Sao_Paulo")

NOMES_LOJAS = {
    "MG - JOAO MONLEVADE - CARNEIRINHOS": "JM",
    "MG - PATROCINIO - CENTRO":           "PATROCINIO",
    "MG - UBÁ - CENTRO":                  "UBA",
    "SP - IBIUNA - CENTRO":               "IBIUNA",
}

LOGIN  = os.environ.get("EVUP_LOGIN", "36217165805")
SENHA  = os.environ.get("EVUP_SENHA", "Alosi9090@@@***")

# Webhook para agendamentos (crie no N8N ou use o mesmo com payload diferente)
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL_AGENDAMENTOS",
    "https://automacaoaryel.app.n8n.cloud/webhook/evup-agendamentos"
)

# True = browser invisível (produção), False = mostra browser (debug)
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"

DOWNLOADS = os.environ.get(
    "DOWNLOADS_DIR",
    os.path.join(os.path.expanduser("~"), "Downloads") if os.name == "nt" else "/tmp"
)
DIAS_FRENTE = 14  # Período do relatório: hoje + 14 dias

# Campos obrigatórios no Nível de Detalhamento
CAMPOS_DETALHAMENTO = [
    "Estabelecimento",
    "Data Agendada",
    "Localidade",
    "Status",
    "Cliente",
    "Usuário do agendamento",
    "Data de Criação",
]


# ================================================================
# HELPERS DE NAVEGAÇÃO
# ================================================================

def clicar(page, seletores, nome, timeout=10000):
    """Tenta clicar em um elemento usando múltiplos seletores."""
    for i, loc in enumerate(seletores, 1):
        try:
            print(f"  [{i}] Clicando em '{nome}'...")
            loc.wait_for(timeout=timeout)
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=timeout)
            print(f"  OK: '{nome}'")
            return True
        except Exception as e:
            print(f"  Falha [{i}]: {e}")
    print(f"  !! Não conseguiu clicar em '{nome}'")
    return False



# ================================================================
# EXPANDIR PAINEL DE FILTRO (barra azul "Filtro")
# ================================================================

def expandir_filtro(page):
    """
    Garante que o painel de filtros está expandido.
    A barra azul "▼ Filtro" precisa ser clicada para mostrar os campos.
    Após o BUSCA ela volta a colapsar — chame esta função novamente antes do EXCEL.
    """
    page.wait_for_timeout(800)

    # Se BUSCA já está visível → filtro já expandido
    try:
        if page.locator("button:has-text('BUSCA')").first.is_visible():
            print("  [FILTRO] Já expandido.")
            return
    except Exception:
        pass

    print("  [FILTRO] Expandindo painel de filtros...")
    ok = clicar(page, [
        page.locator(".card-header:has-text('Filtro')").first,
        page.locator(".panel-heading:has-text('Filtro')").first,
        page.locator("[class*='header']:has-text('Filtro')").first,
        page.locator("[class*='filter']:has-text('Filtro')").first,
        page.locator("*").filter(has_text="Filtro").nth(0),
    ], "barra Filtro", timeout=5000)

    if not ok:
        # Fallback via JS: clica no primeiro elemento com texto "Filtro"
        page.evaluate("""
            () => {
                const all = Array.from(document.querySelectorAll('*'));
                const el = all.find(e =>
                    e.offsetParent !== null &&
                    e.innerText?.trim() === 'Filtro' &&
                    e.children.length <= 5
                );
                if (el) el.click();
            }
        """)

    page.wait_for_timeout(1200)


# ================================================================
# CONFIGURAR NÍVEL DE DETALHAMENTO
# ================================================================

def _abrir_dropdown_nivel(page):
    """Clica na seta ▼ do Select2 'Nível de Detalhamento' para abrir o dropdown."""
    resultado = page.evaluate("""
        () => {
            const labels = Array.from(document.querySelectorAll('label, span, div'));
            const lbl = labels.find(el =>
                el.offsetParent !== null &&
                el.textContent.trim().startsWith('Nível de Detalhamento')
            );
            if (!lbl) return 'label_nao_encontrado';
            let parent = lbl.parentElement;
            for (let i = 0; i < 6; i++) {
                if (!parent) break;
                const choices = parent.querySelector('.select2-choices');
                if (choices) { choices.click(); return 'ok_nivel_' + i; }
                parent = parent.parentElement;
            }
            return 'nao_encontrado';
        }
    """)
    print(f"  [▼] {resultado}")
    page.wait_for_timeout(500)


def configurar_nivel_detalhamento(page):
    """
    Configura o multiselect Select2 'Nível de Detalhamento'.
    Para cada campo: abre o dropdown (▼) → clica no item → repete.
    """
    print("\n[DETALHE] Configurando Nível de Detalhamento...")

    def tags_atuais():
        try:
            return page.evaluate("""
                () => {
                    const labels = Array.from(document.querySelectorAll('label, span, div'));
                    const lbl = labels.find(el =>
                        el.offsetParent !== null &&
                        el.textContent.trim().startsWith('Nível de Detalhamento')
                    );
                    if (!lbl) return [];
                    let p = lbl.parentElement;
                    for (let i = 0; i < 6; i++) {
                        if (!p) break;
                        const tags = p.querySelectorAll('li.select2-search-choice');
                        if (tags.length) return Array.from(tags).map(t => t.textContent.trim());
                        p = p.parentElement;
                    }
                    return [];
                }
            """) or []
        except Exception:
            return []

    for campo in CAMPOS_DETALHAMENTO:
        # Pula se já está selecionado
        if any(campo.lower() in t.lower() for t in tags_atuais()):
            print(f"  '{campo}' já selecionado")
            continue

        print(f"  Selecionando '{campo}'...")

        # 1) Abre o dropdown
        _abrir_dropdown_nivel(page)

        # 2) Clica no item via Playwright (mais confiável que JS para cliques)
        ok = False
        for loc in [
            page.locator(".select2-results li").filter(has_text=campo).first,
            page.locator(f".select2-result:has-text('{campo}')").first,
            page.locator(f"li:visible:has-text('{campo}')").first,
        ]:
            try:
                loc.wait_for(state="visible", timeout=2000)
                loc.click(timeout=2000)
                ok = True
                print(f"  OK: '{campo}'")
                break
            except Exception:
                continue

        if not ok:
            # Fallback JS
            r = page.evaluate(f"""
                () => {{
                    const drops = Array.from(document.querySelectorAll('.select2-drop, .select2-results'));
                    for (const d of drops) {{
                        const items = Array.from(d.querySelectorAll('li'));
                        const item = items.find(li =>
                            li.offsetParent !== null &&
                            li.textContent.trim().toLowerCase().includes('{campo.lower()}')
                        );
                        if (item) {{ item.click(); return 'js_ok'; }}
                    }}
                    return 'nao_encontrado';
                }}
            """)
            print(f"  Fallback JS '{campo}': {r}")

        page.wait_for_timeout(400)

    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    page.screenshot(path="debug_detalhe_ok.png")
    print("  Nível de Detalhamento configurado. Screenshot: debug_detalhe_ok.png")


# ================================================================
# LOGIN
# ================================================================

def login_evup(page):
    print("Abrindo EVUP...")
    page.goto("https://espacolaser.evup.com.br/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    page.get_by_text("Colaborador Franquia", exact=True).click(timeout=15000)
    page.wait_for_timeout(3000)

    page.locator('input[type="text"]').first.fill(LOGIN, timeout=15000)
    page.locator('input[type="password"]').first.fill(SENHA, timeout=15000)
    page.locator('button[type="submit"]').click(timeout=15000)
    page.wait_for_timeout(10000)
    print("Login OK.")


# ================================================================
# NAVEGAÇÃO: Relatórios → Agendamentos
# ================================================================

def navegar_para_agendamentos(page):
    print("\n[NAV] Clicando em Relatórios...")
    page.screenshot(path="debug_01_pos_login.png")

    ok = clicar(page, [
        page.get_by_text("Relatórios", exact=True),
        page.locator("a:has-text('Relatórios')").first,
        page.locator("span:has-text('Relatórios')").first,
        page.locator("li:has-text('Relatórios') > a").first,
        page.locator("text=Relatórios").first,
    ], "Relatórios")

    if not ok:
        raise Exception("Não encontrou o menu 'Relatórios'")

    page.wait_for_timeout(3000)
    page.screenshot(path="debug_02_relatorios.png")

    print("[NAV] Clicando em Agendamentos...")
    ok = clicar(page, [
        page.get_by_text("Agendamentos", exact=True),
        page.locator("a:has-text('Agendamentos')").first,
        page.locator("li:has-text('Agendamentos') > a").first,
        page.locator("text=Agendamentos").first,
    ], "Agendamentos")

    if not ok:
        raise Exception("Não encontrou o submenu 'Agendamentos'")

    page.wait_for_timeout(4000)
    page.screenshot(path="debug_03_agendamentos.png")
    print("[NAV] Na página de Agendamentos. Screenshot: debug_03_agendamentos.png")


# ================================================================
# CONFIGURAR FILTRO DE DATAS
# ================================================================

def configurar_datas(page, inicio_str: str, fim_str: str, data_inicio, data_fim):
    """
    Configura o date range picker 'Data Programada'.
    Triple-click no campo → seleciona tudo → digita o range completo → Enter.
    """
    print(f"\n[DATA] Configurando: {inicio_str} → {fim_str}")
    texto_range = f"{inicio_str} - {fim_str}"

    page.wait_for_timeout(600)

    # Screenshot antes para diagnóstico
    page.screenshot(path="debug_04_antes_data.png")

    # Inspeciona os inputs visíveis para saber qual seletor usar
    info = page.evaluate("""
        () => Array.from(document.querySelectorAll('input'))
            .filter(el => el.offsetParent !== null)
            .map(el => ({
                tag: el.tagName,
                type: el.type,
                cls: el.className.substring(0, 60),
                val: (el.value || '').substring(0, 50),
                name: el.name,
                id: el.id
            }))
    """)
    print(f"  Inputs visíveis: {info}")

    # Tenta encontrar o campo de data por vários seletores
    campo = None
    for sel in [
        "input[name='txtDate']",
        "input[class*='daterange-picker']",
        "input[value*='/']",
        "input[readonly]",
        "input[class*='date']",
        "input[name*='data']",
        "input[name*='date']",
        "input[placeholder*='ata']",
        "input[type='text']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                campo = loc
                print(f"  Campo encontrado via: {sel!r}")
                break
        except Exception:
            continue

    if campo is None:
        raise Exception(
            "Campo de data não encontrado. Verifique debug_04_antes_data.png"
        )

    # Triple-click (click_count=3) → seleciona tudo → digita o range → Enter
    campo.click(click_count=3)
    page.wait_for_timeout(400)
    page.keyboard.type(texto_range, delay=40)
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)

    page.screenshot(path="debug_04_datas.png")
    print(f"  Data configurada. Screenshot: debug_04_datas.png")


# ================================================================
# LIMPAR COLUNA ANTERIOR DA TABELA (× Estabelecimento)
# ================================================================

def limpar_coluna_anterior(page):
    """
    Clica no × do agrupamento '↑ Estabelecimento' na área da tabela (Kendo Grid).
    """
    page.wait_for_timeout(400)

    # Tenta via Playwright primeiro (mais confiável para clicar)
    for sel in [
        "a.k-ungroup",
        ".k-group-indicator a.k-button",
        ".k-group-indicator a:last-of-type",
        ".k-grouping-header a",
        "[class*='ungroup']",
        "[class*='group-delete']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                loc.click(timeout=2000)
                print(f"  [×] Clicado via Playwright: {sel}")
                page.wait_for_timeout(600)
                return
        except Exception:
            continue

    # Fallback JS — percorre toda a área de agrupamento Kendo
    resultado = page.evaluate("""
        () => {
            const seletores = [
                'a.k-ungroup',
                '.k-group-indicator a.k-button',
                '.k-group-indicator a:last-child',
                '.k-grouping-header a',
                '.k-group-panel a',
            ];
            for (const sel of seletores) {
                const el = document.querySelector(sel);
                if (el) { el.click(); return 'js:' + sel; }
            }
            // Último recurso: qualquer elemento com "×" visível
            const all = Array.from(document.querySelectorAll('a, button, span'));
            const x = all.find(el =>
                el.offsetParent !== null &&
                /^[×✕]$/.test(el.textContent.trim())
            );
            if (x) { x.click(); return 'js:x_generico:' + x.className; }
            return 'nao_encontrado';
        }
    """)
    print(f"  [×] JS: {resultado}")
    page.wait_for_timeout(600)


# ================================================================
# BUSCAR E AGUARDAR CARREGAMENTO
# ================================================================

def buscar_e_aguardar(page):
    print("\n[BUSCA] Clicando em Buscar...")
    ok = clicar(page, [
        page.get_by_role("button", name="BUSCA"),
        page.get_by_role("button", name="Buscar"),
        page.get_by_role("button", name="Pesquisar"),
        page.get_by_text("BUSCA", exact=True),
        page.get_by_text("Buscar", exact=True),
        page.locator("button:has-text('BUSCA')").first,
        page.locator("button:has-text('Buscar')").first,
    ], "Buscar")

    if not ok:
        raise Exception("Botão 'Buscar' não encontrado")

    print("[BUSCA] Aguardando carregamento...")
    page.wait_for_timeout(5000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(5000)
    page.screenshot(path="debug_05_resultado.png")
    print("[BUSCA] Resultado carregado. Screenshot: debug_05_resultado.png")


# ================================================================
# DOWNLOAD DO EXCEL
# ================================================================

def baixar_excel(page) -> str:
    """
    Expande o painel Filtro (onde fica o botão EXCEL) e faz o download.
    Após o BUSCA o painel colapsa — é preciso expandir novamente.
    """
    print("\n[EXCEL] Expandindo filtro para acessar botão Excel...")
    expandir_filtro(page)

    page.screenshot(path="debug_06_antes_excel.png")

    # Registra tempo antes do clique para encontrar o arquivo depois
    ts_antes = time.time()

    try:
        # Tenta interceptar o download diretamente
        with page.expect_download(timeout=60000) as download_info:
            clicar(page, [
                page.get_by_role("button", name="EXCEL"),
                page.get_by_role("button", name="Excel"),
                page.get_by_text("EXCEL", exact=True),
                page.get_by_text("Excel", exact=True),
                page.locator("button:has-text('EXCEL')").first,
                page.locator("button:has-text('Excel')").first,
                page.locator("a:has-text('Excel')").first,
                page.locator("[title='Excel']").first,
                page.locator(".k-grid-toolbar button").last,
            ], "EXCEL")

            # Modal de confirmação: "Atenção! A exportação será de todas as linhas..."
            page.wait_for_timeout(1000)
            clicar(page, [
                page.get_by_role("button", name="CONFIRMAR"),
                page.locator("button:has-text('CONFIRMAR')").first,
                page.get_by_text("CONFIRMAR", exact=True),
            ], "CONFIRMAR modal", timeout=5000)

        download = download_info.value
        nome_arquivo = download.suggested_filename or f"Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        caminho = os.path.join(DOWNLOADS, nome_arquivo)
        download.save_as(caminho)
        print(f"[EXCEL] Download salvo: {caminho}")
        return caminho

    except Exception as e:
        print(f"  ⚠️  Interceptação falhou ({e}). Aguardando arquivo na pasta Downloads...")

    # Fallback: procura arquivo novo na pasta Downloads
    print(f"[EXCEL] Aguardando arquivo novo em: {DOWNLOADS}")
    fim = time.time() + 60
    while time.time() < fim:
        arquivos = glob.glob(os.path.join(DOWNLOADS, "Export*.xlsx"))
        novos = [f for f in arquivos if os.path.getmtime(f) > ts_antes]
        if novos:
            caminho = max(novos, key=os.path.getmtime)
            print(f"[EXCEL] Arquivo encontrado: {caminho}")
            return caminho
        time.sleep(2)

    raise Exception("Arquivo Excel não encontrado após 60 segundos")


# ================================================================
# PARSER DO EXCEL
# ================================================================

def detectar_coluna(df: pd.DataFrame, candidatos: list) -> str | None:
    """Encontra a primeira coluna do DataFrame que bate com a lista de candidatos."""
    cols_lower = {str(c).lower().strip(): c for c in df.columns}
    for cand in candidatos:
        c = str(cand).lower().strip()
        if cand in df.columns:
            return cand
        if c in cols_lower:
            return cols_lower[c]
        # busca parcial
        for col_l, col_orig in cols_lower.items():
            if c in col_l or col_l in c:
                return col_orig
    return None


def ler_excel(caminho: str) -> pd.DataFrame:
    """Lê o Excel e retorna um DataFrame limpo."""
    print(f"\n[PARSE] Lendo: {caminho}")
    df = pd.read_excel(caminho, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    print(f"  Linhas: {len(df)} | Colunas: {list(df.columns)}")
    return df


def mapear_colunas(df: pd.DataFrame) -> dict:
    """Detecta os nomes das colunas relevantes."""
    mapa = {
        "estabelecimento": detectar_coluna(df, [
            "Estabelecimento", "Unidade", "Loja", "Franquia"
        ]),
        "data_agendada": detectar_coluna(df, [
            "Data Agendada", "Data do Agendamento", "Data Programada", "Data"
        ]),
        "localidade": detectar_coluna(df, [
            "Localidade", "Tipo de Localidade"
        ]),
        "status": detectar_coluna(df, [
            "Status", "Situação"
        ]),
        "cliente": detectar_coluna(df, [
            "Cliente", "Nome do Cliente", "Paciente"
        ]),
        "usuario": detectar_coluna(df, [
            "Usuário do agendamento", "Usuário", "Responsável", "Atendente", "Consultor"
        ]),
        "data_criacao": detectar_coluna(df, [
            "Data de Criação", "Data Criação", "Criação", "Criado em", "Data de Cadastro"
        ]),
        "tipo_servico": detectar_coluna(df, [
            "Tipo de Serviço", "Serviço", "Tipo", "Procedimento",
            "Tipo Agendamento", "Tipo de Agendamento"
        ]),
    }

    print("\n[PARSE] Mapeamento de colunas:")
    for k, v in mapa.items():
        print(f"  {k:20s} → {v}")

    return mapa


def normalizar_data(serie: pd.Series) -> pd.Series:
    """Converte série de datas para date objects."""
    def _conv(v):
        if pd.isna(v) or str(v).strip() in ("", "nan", "NaT"):
            return None
        try:
            return pd.to_datetime(str(v), dayfirst=True, errors="coerce").date()
        except Exception:
            return None
    return serie.apply(_conv)


# ================================================================
# GERAÇÃO DE MENSAGENS
# ================================================================

def gerar_msg_criados_hoje(df: pd.DataFrame, mapa: dict, data_ref: str, hora_ref: str, hoje) -> str | None:
    """
    Relatório de Criação de Agendamentos — estilo Screenshot 2 do usuário.

    Filtros:
      - Status  ≠ CANCELADO
      - Localidade = AVALIAÇÃO
      - Data de Criação = hoje

    Agrupamento: Estabelecimento → Usuário do agendamento
    """
    col_estab      = mapa.get("estabelecimento")
    col_usuario    = mapa.get("usuario")
    col_status     = mapa.get("status")
    col_localidade = mapa.get("localidade")
    col_criacao    = mapa.get("data_criacao")

    if not col_estab:
        print("[MSG-CRIACAO] Coluna 'Estabelecimento' não encontrada.")
        return None

    df2 = df.copy()

    # 1. Remove cancelados
    if col_status:
        df2 = df2[~df2[col_status].str.upper().str.contains("CANCEL", na=False)]

    # 2. Filtra apenas AVALIAÇÃO
    if col_localidade:
        df2 = df2[df2[col_localidade].str.upper().str.contains("AVALIA", na=False)]
    else:
        print("[MSG-CRIACAO] ⚠️  Coluna 'Localidade' não encontrada — não filtrando por tipo.")

    # 3. Filtra por data de criação = hoje
    if col_criacao:
        df2["_dt_criacao"] = normalizar_data(df2[col_criacao])
        df2 = df2[df2["_dt_criacao"] == hoje]
    else:
        print("[MSG-CRIACAO] ⚠️  Coluna 'Data de Criação' não encontrada.")

    if df2.empty:
        print("[MSG-CRIACAO] Nenhum agendamento de AVALIAÇÃO criado hoje.")
        return None

    linhas = [
        "*📅 Agendamentos Criados Hoje*",
        f"_{data_ref} {hora_ref}_\n",
    ]

    for estab_orig, grp_estab in df2.groupby(col_estab, sort=True):
        nome_curto = NOMES_LOJAS.get(str(estab_orig).strip(), str(estab_orig).strip())
        total_loja = len(grp_estab)

        linhas.append(f"📍 *{nome_curto}* — {total_loja}")

        if col_usuario:
            for usuario, grp_usr in grp_estab.groupby(col_usuario, sort=True):
                linhas.append(f"  {usuario} — {len(grp_usr)}")
        linhas.append("")

    linhas.append(f"*Total Geral: {len(df2)}*")
    return "\n".join(linhas)


def gerar_msg_proximos_dias(df: pd.DataFrame, mapa: dict, data_ref: str, hora_ref: str, hoje) -> str | None:
    """
    Relatório da Agenda — estilo Screenshot 1 do usuário.

    Filtros:
      - Status ≠ CANCELADO
      - Data Agendada ≥ hoje (datas futuras já vêm filtradas no download)

    Agrupamento: Localidade (AVALIAÇÃO / LASER) → Estabelecimento → datas (colunas)
    Datas ordenadas: mais próxima primeiro (ascendente)
    """
    col_estab      = mapa.get("estabelecimento")
    col_data       = mapa.get("data_agendada")
    col_status     = mapa.get("status")
    col_localidade = mapa.get("localidade")

    if not col_estab or not col_data:
        print("[MSG-AGENDA] Colunas obrigatórias não encontradas.")
        return None

    df2 = df.copy()

    # 1. Remove cancelados
    if col_status:
        df2 = df2[~df2[col_status].str.upper().str.contains("CANCEL", na=False)]

    # 2. Converte e filtra datas
    df2["_dt"] = normalizar_data(df2[col_data])
    df2 = df2.dropna(subset=["_dt"])
    df2 = df2[df2["_dt"] >= hoje]

    if df2.empty:
        print("[MSG-AGENDA] Nenhum agendamento futuro encontrado.")
        return None

    # Datas únicas ordenadas ascendente
    datas_unicas = sorted(df2["_dt"].unique())

    # Limita a 10 datas para caber melhor no WhatsApp
    datas_exibir = datas_unicas[:10]
    datas_str    = [d.strftime("%d/%m") for d in datas_exibir]

    def linha_valores(df_grupo):
        """Retorna lista de contagens (string) para cada data."""
        return [
            str(len(df_grupo[df_grupo["_dt"] == d])) if len(df_grupo[df_grupo["_dt"] == d]) > 0 else "-"
            for d in datas_exibir
        ]

    def bloco_localidade(df_loc, nome_loc: str) -> list:
        linhas_bloco = [f"*{nome_loc}*"]

        # Total do tipo por data
        totais = linha_valores(df_loc)
        linhas_bloco.append("  " + " | ".join(v.rjust(4) for v in totais))

        # Por loja
        for estab_orig, grp in df_loc.groupby(col_estab, sort=True):
            nome_curto = NOMES_LOJAS.get(str(estab_orig).strip(), str(estab_orig).strip())
            vals = linha_valores(grp)
            linhas_bloco.append(f"  {nome_curto:<10}" + " | ".join(v.rjust(4) for v in vals))

        return linhas_bloco

    # Monta mensagem
    cab = " | ".join(d.rjust(5) for d in datas_str)
    linhas = [
        "*📆 Agendamentos Próximos Dias*",
        f"_{data_ref} {hora_ref}_\n",
        f"{'':12}{cab}",
        "─" * min(80, 12 + len(cab) + 2),
    ]

    if col_localidade:
        # Ordena: AVALIAÇÃO primeiro, depois LASER, depois outros
        todos_tipos = list(df2[col_localidade].dropna().unique())
        tipos_ordem = []
        for pref in ["AVALIA", "LASER"]:
            for t in todos_tipos:
                if str(t).upper().startswith(pref) and t not in tipos_ordem:
                    tipos_ordem.append(t)
        for t in sorted(todos_tipos):
            if t not in tipos_ordem:
                tipos_ordem.append(t)

        for tipo in tipos_ordem:
            df_tipo = df2[df2[col_localidade] == tipo]
            if not df_tipo.empty:
                linhas += bloco_localidade(df_tipo, str(tipo))
                linhas.append("")
    else:
        linhas += bloco_localidade(df2, "AGENDAMENTOS")
        linhas.append("")

    # Linha de Total Geral
    totais_geral = linha_valores(df2)
    linhas.append("*Total Geral*")
    linhas.append("  " + " | ".join(v.rjust(4) for v in totais_geral))

    return "\n".join(linhas)


# ================================================================
# GERAÇÃO DE IMAGEM — PRÓXIMOS DIAS
# ================================================================

def gerar_imagem_proximos_dias(df: pd.DataFrame, mapa: dict, hoje, data_ref: str, hora_ref: str):
    """
    Gera uma imagem PNG da tabela de Agendamentos Próximos Dias
    no estilo de tabela dinâmica (semelhante ao Excel).
    Retorna (caminho_png, base64_string) ou (None, None) se falhar.
    """
    col_estab      = mapa.get("estabelecimento")
    col_data       = mapa.get("data_agendada")
    col_status     = mapa.get("status")
    col_localidade = mapa.get("localidade")

    if not col_estab or not col_data:
        return None, None

    df2 = df.copy()
    if col_status:
        df2 = df2[~df2[col_status].str.upper().str.contains("CANCEL", na=False)]
    df2["_dt"] = normalizar_data(df2[col_data])
    df2 = df2.dropna(subset=["_dt"])
    df2 = df2[df2["_dt"] >= hoje]

    if df2.empty:
        return None, None

    datas_exibir = sorted(df2["_dt"].unique())[:10]
    datas_col    = [d.strftime("%d/%m/%Y") for d in datas_exibir]

    # ── Monta linhas da tabela ──────────────────────────────────────
    linhas = []  # (label, [val, ...], estilo)

    tipos = []
    if col_localidade:
        for pref, nome in [("AVALIA", "AVALIAÇÃO"), ("LASER", "LASER")]:
            df_t = df2[df2[col_localidade].str.upper().str.startswith(pref, na=False)]
            if not df_t.empty:
                tipos.append((nome, df_t))

    for nome_tipo, df_tipo in tipos:
        totais = [len(df_tipo[df_tipo["_dt"] == d]) for d in datas_exibir]
        linhas.append((nome_tipo, totais, "secao"))

        for estab_orig, grp in df_tipo.groupby(col_estab, sort=True):
            nome = NOMES_LOJAS.get(str(estab_orig).strip(), str(estab_orig).strip())
            vals = [len(grp[grp["_dt"] == d]) for d in datas_exibir]
            linhas.append((nome, vals, "loja"))

    totais_geral = [len(df2[df2["_dt"] == d]) for d in datas_exibir]
    linhas.append(("Total Geral", totais_geral, "total"))

    # ── Dimensões ───────────────────────────────────────────────────
    n_datas   = len(datas_exibir)
    label_w   = 3.0
    date_w    = 1.15
    row_h     = 0.40
    header_h  = 0.50
    pad       = 0.25

    fig_w = pad + label_w + date_w * n_datas + pad
    fig_h = pad + header_h + row_h * len(linhas) + pad + 0.45

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    # ── Cores ────────────────────────────────────────────────────────
    COR = {
        "cabecalho": ("#4472C4", "white"),
        "secao_avalia": ("#E2EFDA", "black"),
        "secao_laser":  ("#DEEAF1", "black"),
        "loja_par":  ("white",   "black"),
        "loja_impar":("#F5F5F5", "black"),
        "total":     ("#D9D9D9", "black"),
    }

    def celula(x, y, w, h, texto, bg, fg, bold=False, alinha="center"):
        ax.add_patch(plt.Rectangle(
            (x, y), w, h,
            facecolor=bg, edgecolor="#BBBBBB", linewidth=0.5, zorder=1
        ))
        tx  = x + 0.12 if alinha == "left" else x + w / 2
        ax.text(tx, y + h / 2, texto,
                ha=alinha if alinha == "left" else "center",
                va="center", fontsize=8.5, color=fg,
                fontweight="bold" if bold else "normal",
                clip_on=True, zorder=2)

    # ── Cabeçalho ────────────────────────────────────────────────────
    y_cab = fig_h - pad - header_h
    celula(pad, y_cab, label_w, header_h,
           "Rótulos de Linha", COR["cabecalho"][0], COR["cabecalho"][1], True, "left")
    for i, d in enumerate(datas_col):
        celula(pad + label_w + i * date_w, y_cab, date_w, header_h,
               d, COR["cabecalho"][0], COR["cabecalho"][1], True)

    # ── Linhas de dados ───────────────────────────────────────────────
    loja_idx = 0
    for ri, (label, vals, estilo) in enumerate(linhas):
        y_row = fig_h - pad - header_h - (ri + 1) * row_h

        if estilo == "secao":
            chave_bg = "secao_avalia" if "AVALIA" in label.upper() else "secao_laser"
            bg, fg, bold = COR[chave_bg][0], COR[chave_bg][1], True
            loja_idx = 0
        elif estilo == "total":
            bg, fg, bold = COR["total"][0], COR["total"][1], True
        else:
            chave_bg = "loja_par" if loja_idx % 2 == 0 else "loja_impar"
            bg, fg, bold = COR[chave_bg][0], COR[chave_bg][1], False
            loja_idx += 1

        texto_label = f"  {label}" if estilo == "loja" else label
        celula(pad, y_row, label_w, row_h, texto_label, bg, fg, bold, "left")

        for i, v in enumerate(vals):
            celula(pad + label_w + i * date_w, y_row, date_w, row_h,
                   str(v) if v > 0 else "", bg, fg, bold)

    # ── Título ───────────────────────────────────────────────────────
    ax.text(fig_w / 2, fig_h - pad / 2,
            f"Agendamentos Próximos Dias  —  {data_ref}  {hora_ref}",
            ha="center", va="center", fontsize=10.5,
            fontweight="bold", color="#333333")

    # ── Salva ────────────────────────────────────────────────────────
    caminho = os.path.join(DOWNLOADS, f"agenda_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    print(f"[IMG] Salvando em: {caminho}")
    plt.savefig(caminho, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()

    if not os.path.exists(caminho):
        print(f"[IMG] ERRO: arquivo não foi criado em {caminho}")
        return None, None

    with open(caminho, "rb") as f:
        img_b64 = "data:image/png;base64," + base64.b64encode(f.read()).decode()

    print(f"[IMG] Imagem gerada com sucesso: {caminho}")
    return caminho, img_b64


# ================================================================
# ENVIO WEBHOOK
# ================================================================

def enviar_webhook(mensagem: str, tipo: str, data_ref: str, hora_ref: str, imagem_b64: str = None):
    payload = {
        "tipo": tipo,
        "data": data_ref,
        "hora": hora_ref,
        "mensagem_whatsapp": mensagem,
    }
    if imagem_b64:
        payload["imagem_base64"] = imagem_b64
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=60)
        print(f"  Webhook [{tipo}]: {resp.status_code}")
    except Exception as e:
        print(f"  ERRO webhook [{tipo}]: {e}")


# ================================================================
# MAIN
# ================================================================

def horario_permitido() -> bool:
    """Verifica se o horário atual está dentro da grade permitida."""
    agora = datetime.now(TZ_SP)
    dia   = agora.weekday()   # 0=Seg … 5=Sáb, 6=Dom
    hora  = agora.hour
    if dia == 6:              # Domingo — nunca executa
        return False
    if dia == 5:              # Sábado — só 10h, 12h, 14h, 16h
        return hora in [10, 12, 14, 16]
    return hora in [10, 12, 14, 16, 18, 20]  # Seg–Sex


def main():
    if not horario_permitido():
        agora = datetime.now(TZ_SP)
        print(f"[{agora.strftime('%d/%m/%Y %H:%M')}] Fora do horário permitido. Encerrando.")
        return

    agora_sp   = datetime.now(TZ_SP)
    data_ref   = agora_sp.strftime("%d/%m/%Y")
    hora_ref   = agora_sp.strftime("%H:%M")
    hoje       = agora_sp.date()
    data_fim   = hoje + timedelta(days=DIAS_FRENTE)
    inicio_str = hoje.strftime("%d/%m/%Y")
    fim_str    = data_fim.strftime("%d/%m/%Y")

    print(f"\n{'='*55}")
    print(f"  Bot Agendamentos — {data_ref} {hora_ref}")
    print(f"  Período: {inicio_str} → {fim_str}")
    print(f"  Headless: {HEADLESS}")
    print(f"{'='*55}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            accept_downloads=True,
        )
        page = context.new_page()

        try:
            # ── 1. Login ────────────────────────────────────────────
            login_evup(page)

            # ── 2. Relatórios → Agendamentos ───────────────────────
            navegar_para_agendamentos(page)

            # ── 3. Expande o painel Filtro (começa colapsado) ───────
            expandir_filtro(page)

            # ── 4. Configura Data Programada ────────────────────────
            configurar_datas(page, inicio_str, fim_str, hoje, data_fim)

            # ── 5. Configura Nível de Detalhamento ──────────────────
            configurar_nivel_detalhamento(page)

            # ── 6. Remove coluna anterior (× Estabelecimento) ───────
            limpar_coluna_anterior(page)

            # ── 7. Clica no Filtro (obrigatório após o ×) ────────────
            clicar(page, [
                page.locator(".card-header:has-text('Filtro')").first,
                page.locator(".panel-heading:has-text('Filtro')").first,
                page.locator("[class*='header']:has-text('Filtro')").first,
                page.locator("*").filter(has_text="Filtro").nth(0),
            ], "Filtro (após ×)", timeout=5000)
            page.wait_for_timeout(800)

            # ── 8. Download Excel ────────────────────────────────────
            caminho_excel = baixar_excel(page)
            browser.close()

        except Exception as e:
            print(f"\nERRO CRÍTICO: {e}")
            try:
                page.screenshot(path="debug_erro_geral.png")
            except Exception:
                pass
            browser.close()
            print(f"\n[{datetime.now(TZ_SP).strftime('%H:%M:%S')}] Encerrado com erro.\n")
            return

    # ── 6. Parsear Excel ────────────────────────────────────────
    try:
        df   = ler_excel(caminho_excel)
        mapa = mapear_colunas(df)
    except Exception as e:
        print(f"\nERRO ao ler Excel: {e}")
        return

    # ── 7a. Mensagem: Criados Hoje ──────────────────────────────
    print("\n--- GERANDO: Criados Hoje ---")
    msg1 = gerar_msg_criados_hoje(df, mapa, data_ref, hora_ref, hoje)
    if msg1:
        print(msg1)
        enviar_webhook(msg1, "criados_hoje", data_ref, hora_ref)
    else:
        print("  Sem dados para 'Criados Hoje'.")

    # ── 7b. Mensagem: Próximos Dias (imagem) ────────────────────
    print("\n--- GERANDO: Próximos Dias ---")
    try:
        caminho_img, img_b64 = gerar_imagem_proximos_dias(df, mapa, hoje, data_ref, hora_ref)
    except Exception as e:
        print(f"  ERRO ao gerar imagem: {e}")
        caminho_img, img_b64 = None, None

    msg2 = gerar_msg_proximos_dias(df, mapa, data_ref, hora_ref, hoje)  # fallback texto
    if caminho_img:
        print(f"  Imagem: {caminho_img}")
        enviar_webhook(msg2 or "", "proximos_dias", data_ref, hora_ref, img_b64)
    elif msg2:
        print(msg2)
        enviar_webhook(msg2, "proximos_dias", data_ref, hora_ref)
    else:
        print("  Sem dados para 'Próximos Dias'.")

    print(f"\n[{datetime.now(TZ_SP).strftime('%H:%M:%S')}] Concluído.\n")


if __name__ == "__main__":
    main()
