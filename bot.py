from playwright.sync_api import sync_playwright
from datetime import datetime
import requests
import json
import os
import hashlib

# ===============================
# CREDENCIAIS EVUP
# ===============================
LOGIN = os.environ.get("EVUP_LOGIN", "36217165805")
SENHA = os.environ.get("EVUP_SENHA", "Alosi9090@@@***")

# ===============================
# WEBHOOK N8N
# ===============================
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://automacaoaryel.app.n8n.cloud/webhook/evup-relatorio-vendas")

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
    tentativas = [
        page.get_by_text("Vendas", exact=True),
        page.locator("text=Vendas").first,
        page.locator("a:has-text('Vendas')").first,
    ]
    for i, locator in enumerate(tentativas, start=1):
        try:
            print(f"Tentativa {i} para clicar em 'Vendas'...")
            locator.wait_for(timeout=10000)
            locator.scroll_into_view_if_needed(timeout=5000)
            locator.click(timeout=10000)
            print("OK: clique em Vendas.")
            return
        except Exception as e:
            print(f"Falhou tentativa {i}: {e}")
    raise Exception("Não conseguiu clicar em Vendas")


def clicar_busca(page):
    tentativas = [
        page.get_by_role("button", name="BUSCA"),
        page.get_by_text("BUSCA", exact=True),
        page.locator("button:has-text('BUSCA')").first,
    ]
    for i, locator in enumerate(tentativas, start=1):
        try:
            print(f"Tentativa {i} para clicar em 'BUSCA'...")
            locator.wait_for(timeout=10000)
            locator.scroll_into_view_if_needed(timeout=5000)
            locator.click(timeout=10000)
            print("OK: clique em BUSCA.")
            return
        except Exception as e:
            print(f"Falhou tentativa {i}: {e}")
    raise Exception("Não conseguiu clicar em BUSCA")


def extrair_dados_tabela(page):
    print("Extraindo dados da tabela...")
    page.wait_for_timeout(3000)

    dados = page.evaluate("""
        () => {
            const resultado = [];
            const seletores = [
                'table tbody tr',
                '.k-grid-content tr',
                '.k-grid tbody tr',
                'tr[role="row"]',
                'tbody tr'
            ];

            let rows = [];
            for (const sel of seletores) {
                const found = document.querySelectorAll(sel);
                if (found.length > 0) {
                    rows = Array.from(found);
                    break;
                }
            }

            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length >= 6) {
                    const loja = cells[1]?.innerText?.trim();
                    const valor = cells[5]?.innerText?.trim();
                    if (loja && loja.length > 2 && valor !== undefined) {
                        resultado.push({ loja: loja, valor_liquido: valor || 'R$0,00' });
                    }
                }
            });

            let total = '';
            const footerSelectors = ['tfoot tr', '.k-grid-footer tr', '.k-footer-template'];
            for (const sel of footerSelectors) {
                const footer = document.querySelector(sel);
                if (footer) {
                    const footerCells = footer.querySelectorAll('td');
                    if (footerCells.length >= 6) {
                        total = footerCells[5]?.innerText?.trim() || '';
                        break;
                    }
                }
            }

            return { lojas: resultado, total: total };
        }
    """)

    return dados


def formatar_mensagem(dados, data_ref):
    lojas = dados.get("lojas", [])
    total = dados.get("total", "")

    linhas = [f"📊 *Relatório de Vendas - {data_ref}*\n"]

    for item in lojas:
        linhas.append(f"🏪 {item['loja']}")
        linhas.append(f"💰 {item['valor_liquido']}\n")

    if total:
        linhas.append("─────────────────")
        linhas.append(f"💵 *TOTAL: {total}*")

    return "\n".join(linhas)


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando verificação...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,  # invisível para rodar na nuvem
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
        except Exception as e:
            print("Erro no login:", e)
            browser.close()
            return

        page.wait_for_timeout(10000)

        # 4) Relatórios
        try:
            clicar_relatorios(page)
        except Exception as e:
            print("Erro ao clicar em Relatórios:", e)
            browser.close()
            return

        page.wait_for_timeout(4000)

        # 5) Vendas
        try:
            clicar_vendas(page)
        except Exception as e:
            print("Erro ao clicar em Vendas:", e)
            browser.close()
            return

        page.wait_for_timeout(5000)

        # 6) BUSCA
        try:
            clicar_busca(page)
        except Exception as e:
            print("Erro ao clicar em BUSCA:", e)
            browser.close()
            return

        page.wait_for_timeout(6000)

        # 7) Extrair dados
        data_ref = datetime.now().strftime("%d/%m/%Y")
        dados = extrair_dados_tabela(page)
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
    mensagem = formatar_mensagem(dados, data_ref)
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
