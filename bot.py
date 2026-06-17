from playwright.sync_api import sync_playwright
from datetime import datetime
import pytz
import requests
import json
import os
import hashlib

# ===============================
# CREDENCIAIS EVUP
# ===============================
LOGIN = os.environ.get("EVUP_LOGIN", "36217165805")
SENHA = os.environ.get("EVUP_SENHA", "Alosi9090@@@****")

# ===============================
# WEBHOOK N8N
# ===============================
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://automacaoaryel.app.n8n.cloud/webhook/evup-relatorio-vendas")

# ===============================
# TIMEZONE SAO PAULO
# ===============================
TZ_SP = pytz.timezone("America/Sao_Paulo")

# ===============================
# NOMES CURTOS DAS LOJAS
# ===============================
NOMES_LOJAS = {
    "MG - JOAO MONLEVADE - CARNEIRINHOS": "JM",
    "MG - PATROCINIO - CENTRO": "PATROCINIO",
    "MG - UBÁ - CENTRO": "UBA",
    "MG - UBA - CENTRO": "UBA",
    "SP - IBIUNA - CENTRO": "IBIUNA",
}

# ===============================
# ARQUIVO DE ESTADO (evita duplicatas)
# ===============================
STATE_FILE = "state.json"


def carregar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def salvar_estado(data_ref, hash_dados):
    with open(STATE_FILE, "w") as f:
        json.dump({"data": data_ref, "hash": hash_dados}, f)


def ja_enviado(data_ref, hash_dados):
    estado = carregar_estado()
    return estado.get("data") == data_ref and estado.get("hash") == hash_dados


def gerar_hash(lojas):
    conteudo = json.dumps(lojas, sort_keys=True)
    return hashlib.md5(conteudo.encode()).hexdigest()


def nome_curto(loja):
    for chave, apelido in NOMES_LOJAS.items():
        if chave.upper() in loja.upper():
            return apelido
    return loja


def valor_para_numero(valor_str):
    try:
        return float(
            valor_str.replace("R$", "").replace(".", "").replace(",", ".").strip()
        )
    except Exception:
        return 0.0


def clicar_relatorios(page):
    tentativas = [
        page.get_by_text("Relatórios", exact=True),
        page.locator("a:has-text('Relatórios')").first,
        page.locator("span:has-text('Relatórios')").first,
        page.locator("text=Relatórios").first,
    ]
    for i, locator in enumerate(tentativas, start=1):
        try:
            print(f"Tentativa {i} para clicar em 'Relatórios'...")
            locator.wait_for(timeout=10000)
            locator.scroll_into_view_if_needed(timeout=5000)
            locator.click(timeout=10000)
            print("OK: clique em Relatórios.")
            return
        except Exception as e:
            print(f"Falhou tentativa {i}: {e}")
    raise Exception("Não conseguiu clicar em Relatórios")


def clicar_vendas(page):
    # Aguarda a página de relatórios carregar completamente
    page.wait_for_timeout(3000)

    # Tenta clicar em "Vendas" (exato) com force para elementos ocultos
    seletores = [
        ("get_by_text_exact", None),
        ("a_has_text", None),
        ("text_locator", None),
    ]

    # Tentativa 1: texto exato "Vendas" com force
    try:
        print("Tentativa 1 para clicar em 'Vendas' (force)...")
        locator = page.get_by_text("Vendas", exact=True).first
        locator.scroll_into_view_if_needed(timeout=5000)
        locator.click(timeout=10000, force=True)
        print("OK: clique em Vendas.")
        return
    except Exception as e:
        print(f"Falhou tentativa 1: {e}")

    # Tentativa 2: link com texto exato "Vendas" via JavaScript
    try:
        print("Tentativa 2 para clicar em 'Vendas' (JS)...")
        page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a'));
                const vendas = links.find(a => a.textContent.trim() === 'Vendas');
                if (vendas) { vendas.click(); return true; }
                return false;
            }
        """)
        page.wait_for_timeout(2000)
        print("OK: clique em Vendas via JS.")
        return
    except Exception as e:
        print(f"Falhou tentativa 2: {e}")

    # Tentativa 3: link "Vendas" dentro da seção VENDA
    try:
        print("Tentativa 3 para clicar em 'Vendas' (seção VENDA)...")
        locator = page.locator("a:has-text('Vendas')").first
        locator.click(timeout=10000, force=True)
        print("OK: clique em Vendas (seção VENDA).")
        return
    except Exception as e:
        print(f"Falhou tentativa 3: {e}")

    raise Exception("Não conseguiu clicar em Vendas")


def encontrar_frame_relatorio(page):
    """Encontra o iframe que contém o relatório de vendas"""
    page.wait_for_timeout(3000)
    frames = page.frames
    print(f"Total de frames: {len(frames)}")
    for i, f in enumerate(frames):
        print(f"  Frame {i}: {f.url[:80]}")

    # Prioridade 1: frame com ReportMain na URL (relatório de vendas)
    for f in frames:
        if "ReportMain" in f.url or "reportmain" in f.url.lower():
            print(f"Frame ReportMain encontrado: {f.url[:80]}")
            return f

    # Prioridade 2: qualquer frame com URL do evup que não seja a página principal
    for f in frames:
        if "evup.com.br" in f.url and f.url != frames[0].url and "about:blank" not in f.url:
            print(f"Frame evup secundário encontrado: {f.url[:80]}")
            return f

    print("Nenhum frame do relatório encontrado, usando página principal.")
    return page


def clicar_busca(page):
    # Tentativa 1: botão por role
    try:
        print("Tentativa 1 para clicar em 'BUSCA'...")
        locator = page.get_by_role("button", name="BUSCA").first
        locator.scroll_into_view_if_needed(timeout=5000)
        locator.click(timeout=10000, force=True)
        print("OK: clique em BUSCA.")
        return
    except Exception as e:
        print(f"Falhou tentativa 1: {e}")

    # Tentativa 2: botão com texto BUSCA (force)
    try:
        print("Tentativa 2 para clicar em 'BUSCA' (force)...")
        locator = page.locator("button:has-text('BUSCA')").first
        locator.click(timeout=10000, force=True)
        print("OK: clique em BUSCA (force).")
        return
    except Exception as e:
        print(f"Falhou tentativa 2: {e}")

    # Tentativa 3: aguarda mais e tenta via JavaScript
    try:
        print("Tentativa 3 para clicar em 'BUSCA' (JS + espera extra)...")
        page.wait_for_timeout(5000)

        # Diagnóstico: lista todos os botões visíveis
        botoes = page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll('button, a, input[type=submit], [role=button]'));
                return els.map(el => el.textContent.trim().substring(0, 50) + ' | ' + el.tagName + ' | ' + el.className.substring(0, 30));
            }
        """)
        print("Botões na página:")
        for b in botoes[:20]:
            print(" ", b)

        clicou = page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll('button, a, input[type=submit], [role=button]'));
                const busca = els.find(el => el.textContent.trim().toUpperCase().includes('BUSCA'));
                if (busca) { busca.click(); return true; }
                return false;
            }
        """)
        if clicou:
            print("OK: clique em BUSCA via JS.")
            return
        print("JS não encontrou o botão BUSCA.")
    except Exception as e:
        print(f"Falhou tentativa 3: {e}")

    raise Exception("Não conseguiu clicar em BUSCA")


def extrair_dados_tabela(page):
    print("Extraindo dados da tabela...")

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    page.wait_for_timeout(5000)

    # Diagnóstico: mostra estrutura da tabela
    diagnostico = page.evaluate("""
        () => {
            const resultado = [];
            // Testa diferentes seletores
            const seletores = ['tbody tr', 'table tr', '.k-grid-content tr',
                               '[class*=row]', '[class*=grid] tr', 'tr'];
            for (const sel of seletores) {
                const els = document.querySelectorAll(sel);
                resultado.push(sel + ': ' + els.length + ' elementos');
            }
            // Pega primeiros 3 trs para ver estrutura
            const trs = document.querySelectorAll('tr');
            if (trs.length > 0) {
                resultado.push('Primeiro TR innerHTML: ' + trs[0].innerHTML.substring(0, 200));
            }
            return resultado;
        }
    """)
    print("Diagnóstico tabela:")
    for d in diagnostico:
        print(" ", d)

    for tentativa in range(3):
        dados = page.evaluate("""
            () => {
                const resultado = [];
                const rows = document.querySelectorAll('tbody tr');

                rows.forEach(row => {
                    const cells = Array.from(row.querySelectorAll('td'));
                    if (cells.length < 10) return;

                    const texts = cells.map(c => c.innerText?.trim() || '');

                    const loja = texts.find(t => /^[A-Z]{2}\\s+-\\s+/.test(t));
                    if (!loja) return;

                    let grupoAtual = [];
                    let ultimoGrupo = [];

                    for (const t of texts) {
                        if (t.startsWith('R$')) {
                            grupoAtual.push(t);
                        } else {
                            if (grupoAtual.length > 0) {
                                ultimoGrupo = [...grupoAtual];
                                grupoAtual = [];
                            }
                        }
                    }
                    if (grupoAtual.length > 0) ultimoGrupo = grupoAtual;

                    if (ultimoGrupo.length >= 1) {
                        const valorLiquido = ultimoGrupo[ultimoGrupo.length - 1];
                        resultado.push({ loja: loja, valor_liquido: valorLiquido });
                    }
                });

                let total = '';
                const footerRows = document.querySelectorAll('.k-grid-footer tr, tfoot tr');
                footerRows.forEach(row => {
                    const cells = Array.from(row.querySelectorAll('td'));
                    const texts = cells.map(c => c.innerText?.trim() || '');
                    let grupoAtual = [];
                    let ultimoGrupo = [];
                    for (const t of texts) {
                        if (t.startsWith('R$')) {
                            grupoAtual.push(t);
                        } else {
                            if (grupoAtual.length > 0) {
                                ultimoGrupo = [...grupoAtual];
                                grupoAtual = [];
                            }
                        }
                    }
                    if (grupoAtual.length > 0) ultimoGrupo = grupoAtual;
                    if (ultimoGrupo.length >= 1 && !total) {
                        total = ultimoGrupo[ultimoGrupo.length - 1];
                    }
                });

                return { lojas: resultado, total: total, rowsFound: rows.length };
            }
        """)

        lojas = dados.get("lojas", [])
        rows_found = dados.get("rowsFound", 0)
        print(f"Tentativa {tentativa+1}: {rows_found} linhas, {len(lojas)} lojas")

        if lojas:
            return dados

        page.wait_for_timeout(4000)

    return dados


def formatar_mensagem(dados, data_ref, hora_ref):
    lojas = dados.get("lojas", [])
    total = dados.get("total", "")

    # Ordena por valor decrescente
    lojas_ordenadas = sorted(lojas, key=lambda x: valor_para_numero(x["valor_liquido"]), reverse=True)

    linhas = [f"*Relatório de Vendas {data_ref} {hora_ref}*\n"]

    for item in lojas_ordenadas:
        apelido = nome_curto(item["loja"])
        valor = item["valor_liquido"]
        linhas.append(f"{apelido} - {valor}")

    if total:
        linhas.append(f"\nTotal - {total}")

    return "\n".join(linhas)


def main():
    agora_sp = datetime.now(TZ_SP)
    print(f"[{agora_sp.strftime('%H:%M:%S')}] Iniciando verificação...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()

        # 1) Abrir EVUP
        page.goto("https://espacolaser.evup.com.br/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # 2) Colaborador Franquia
        try:
            page.get_by_text("Colaborador Franquia", exact=True).click(timeout=15000)
            print("OK: clique em Colaborador Franquia.")
        except Exception as e:
            print("Erro ao clicar em Colaborador Franquia:", e)
            browser.close()
            return

        page.wait_for_timeout(3000)

        # 3) Login
        try:
            page.locator('input[type="text"]').first.fill(LOGIN, timeout=15000)
            page.locator('input[type="password"]').first.fill(SENHA, timeout=15000)
            page.locator('button[type="submit"]').click(timeout=15000)
            print("OK: login enviado.")
        except Exception as e:
            print("Erro no login:", e)
            browser.close()
            return

        page.wait_for_timeout(15000)

        # Diagnóstico pós-login
        url_atual = page.url
        titulo = page.title()
        print(f"URL após login: {url_atual}")
        print(f"Título da página: {titulo}")
        texto_pagina = page.inner_text("body")[:500]
        print(f"Texto visível (500 chars): {texto_pagina}")

        # 4) Relatórios
        try:
            clicar_relatorios(page)
        except Exception as e:
            print("Erro ao clicar em Relatórios:", e)
            browser.close()
            return

        page.wait_for_timeout(4000)

        # 5) Encontra Frame 2 (lista de relatórios) e clica em Vendas dentro dele
        frame_lista = encontrar_frame_relatorio(page)
        try:
            clicar_vendas(frame_lista)
        except Exception as e:
            print("Erro ao clicar em Vendas:", e)
            browser.close()
            return

        page.wait_for_timeout(6000)

        # 6) Aguarda novo frame abrir (aba REL. VENDAS com filtro e BUSCA)
        url_frame0 = page.frames[0].url
        frame_vendas = None
        for tentativa in range(6):
            frames = page.frames
            print(f"Aguardando frame de vendas... {len(frames)} frames")
            for i, f in enumerate(frames):
                print(f"  Frame {i}: {f.url[:80]}")

            for f in frames:
                try:
                    # Ignora Frame 0 (página principal) e frames em branco
                    if f.url == url_frame0 or "about:blank" in f.url or "ReportMain/Index" in f.url:
                        continue
                    # Procura frame com URL do evup e botão BUSCA exato
                    if "evup.com.br" in f.url:
                        count = f.locator("button").filter(has_text="BUSCA").count()
                        print(f"  Frame {f.url[:60]}: {count} botão(ões) BUSCA")
                        if count > 0:
                            print(f"Frame de vendas encontrado: {f.url[:80]}")
                            frame_vendas = f
                            break
                except Exception as ex:
                    print(f"  Erro ao checar frame: {ex}")
            if frame_vendas:
                break
            page.wait_for_timeout(3000)

        if not frame_vendas:
            print("Frame de vendas não encontrado. Listando todos os frames:")
            for i, f in enumerate(page.frames):
                print(f"  Frame {i}: {f.url}")
            frame_vendas = frame_lista

        # 7) BUSCA dentro do frame de vendas
        try:
            clicar_busca(frame_vendas)
        except Exception as e:
            print("Erro ao clicar em BUSCA:", e)
            browser.close()
            return

        page.wait_for_timeout(8000)

        # 8) Extrair dados do frame de vendas
        agora_sp = datetime.now(TZ_SP)
        data_ref = agora_sp.strftime("%d/%m/%Y")
        hora_ref = agora_sp.strftime("%H:%M")
        dados = extrair_dados_tabela(frame_vendas)
        browser.close()

    # 8) Verificar se há vendas
    lojas = dados.get("lojas", [])

    if not lojas:
        print("Sem vendas no momento. Nada enviado.")
        return

    # 9) Verificar se já enviou esses mesmos dados
    hash_atual = gerar_hash(lojas)
    if ja_enviado(data_ref, hash_atual):
        print("Dados já enviados anteriormente. Nada a fazer.")
        return

    # 10) Formatar e enviar
    mensagem = formatar_mensagem(dados, data_ref, hora_ref)
    print(f"{len(lojas)} lojas com venda encontradas:")
    for item in lojas:
        print(f"  {item['loja']}: {item['valor_liquido']}")

    print("\nEnviando para o n8n...")
    try:
        payload = {
            "data": data_ref,
            "lojas": lojas,
            "total": dados.get("total", ""),
            "mensagem_whatsapp": mensagem
        }
        response = requests.post(WEBHOOK_URL, json=payload, timeout=60)
        print("Resposta:", response.status_code, response.text)

        # 11) Salvar estado para não duplicar
        salvar_estado(data_ref, hash_atual)
        print("Estado salvo. Próxima execução só envia se houver novos dados.")

    except Exception as e:
        print("Erro ao enviar para o Webhook:", e)

    print("Concluído.")


if __name__ == "__main__":
    main()
