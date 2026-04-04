"""
Token Helper — Espaço Laser Instagram
======================================
Script interativo para gerar e salvar tokens de acesso do Instagram.

Pré-requisitos:
  - App criado no Meta for Developers (developers.facebook.com)
  - App configurado com produtos: Instagram Graph API + Facebook Login
  - Contas do Instagram configuradas como "Profissional" (Empresa ou Criador)
  - Cada conta do Instagram vinculada a uma Página do Facebook

Como usar:
  python token_helper.py
"""

import json
import webbrowser
from pathlib import Path
import requests

# ─── App Meta (mantém o mesmo do renovar_tokens.py original) ──────────────────
APP_ID     = "1863630237659921"
APP_SECRET = "6c398d2750053b77a24fd343724c4801"

TOKENS_FILE    = "tokens.json"
REDIRECT_URI   = "https://www.facebook.com/connect/login_success.html"
GRAPH_VERSION  = "v19.0"
GRAPH_BASE     = f"https://graph.facebook.com/{GRAPH_VERSION}"

SCOPES = ",".join([
    "instagram_basic",
    "instagram_content_publish",
    "pages_read_engagement",
    "pages_show_list",
    "business_management",
])

# Mapeamento nome Instagram → chave interna
CONTAS = {
    "espacolaser.uba":         "espacolaser.uba",
    "espacolaser.ibiuna":      "espacolaser.ibiuna",
    "espacolaser.monlevade":   "espacolaser.monlevade",
    "espacolaser.patrociniomg": "espacolaser.patrociniomg",
}


def separador(titulo: str = ""):
    print("\n" + "─" * 60)
    if titulo:
        print(f"  {titulo}")
        print("─" * 60)


def carregar_ou_criar_config() -> dict:
    """Carrega tokens.json existente ou cria um novo."""
    if Path(TOKENS_FILE).exists():
        with open(TOKENS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # Garante que todas as contas existam
        for nome in CONTAS:
            data["accounts"].setdefault(nome, {"token": "", "ig_user_id": ""})
        return data

    return {
        "app_id":       APP_ID,
        "app_secret":   APP_SECRET,
        "imgbb_api_key": "",
        "accounts": {nome: {"token": "", "ig_user_id": ""} for nome in CONTAS},
    }


def salvar_config(data: dict):
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\n  ✓ Configuração salva em '{TOKENS_FILE}'")


def obter_token_via_oauth() -> str | None:
    """
    Guia o usuário pelo fluxo OAuth para obter um token de acesso.
    Retorna o token de longa duração ou None em caso de falha.
    """
    separador("PASSO 1 — Autorizar acesso no Facebook")

    oauth_url = (
        f"https://www.facebook.com/dialog/oauth"
        f"?client_id={APP_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPES}"
        f"&response_type=code"
    )

    print(f"""
  Será aberta uma janela do navegador para você fazer login no Facebook
  e autorizar o acesso às páginas vinculadas ao Instagram.

  Se o navegador não abrir, acesse manualmente:
  {oauth_url}
""")
    input("  Pressione ENTER para abrir o navegador...")
    webbrowser.open(oauth_url)

    print("""
  Após autorizar, o navegador vai redirecionar para uma página do Facebook
  com uma URL parecida com:

  https://www.facebook.com/connect/login_success.html?code=XXXXXX#_=_

  Copie essa URL COMPLETA e cole abaixo.
""")
    redirect_url = input("  Cole a URL de redirecionamento aqui: ").strip()

    # Extrai o código da URL
    if "?code=" in redirect_url:
        code = redirect_url.split("?code=")[1].split("&")[0].split("#")[0]
    else:
        print("  [ERRO] Não consegui extrair o código da URL.")
        return None

    print(f"\n  Código obtido: {code[:20]}... Trocando por token...")

    # Troca o código pelo token de curta duração
    r = requests.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "client_id":     APP_ID,
            "redirect_uri":  REDIRECT_URI,
            "client_secret": APP_SECRET,
            "code":          code,
        },
        timeout=15,
    )
    data = r.json()
    if "access_token" not in data:
        print(f"  [ERRO] Falha ao trocar código: {data}")
        return None

    token_curto = data["access_token"]
    print("  Token de curta duração obtido. Convertendo para longa duração...")

    # Converte para token de longa duração (~60 dias)
    r2 = requests.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type":        "fb_exchange_token",
            "client_id":         APP_ID,
            "client_secret":     APP_SECRET,
            "fb_exchange_token": token_curto,
        },
        timeout=15,
    )
    data2 = r2.json()
    if "access_token" not in data2:
        print(f"  [ERRO] Falha ao converter token: {data2}")
        return None

    token_longo = data2["access_token"]
    expira_em = data2.get("expires_in", "desconhecido")
    print(f"  ✓ Token de longa duração obtido! Expira em: {expira_em} segundos")
    return token_longo


def listar_paginas_e_contas_ig(user_token: str) -> list[dict]:
    """
    Retorna lista de Páginas do Facebook com suas contas do Instagram vinculadas.
    """
    separador("PASSO 2 — Identificando Páginas e contas do Instagram")

    r = requests.get(
        f"{GRAPH_BASE}/me/accounts",
        params={"access_token": user_token, "fields": "id,name,access_token"},
        timeout=15,
    )
    data = r.json()

    if "data" not in data:
        print(f"  [ERRO] Não foi possível listar páginas: {data}")
        return []

    paginas = data["data"]
    print(f"  {len(paginas)} página(s) encontrada(s):")

    resultados = []
    for pg in paginas:
        pg_id     = pg["id"]
        pg_nome   = pg["name"]
        pg_token  = pg["access_token"]

        # Busca a conta do Instagram vinculada à página
        r2 = requests.get(
            f"{GRAPH_BASE}/{pg_id}",
            params={
                "fields":       "instagram_business_account",
                "access_token": pg_token,
            },
            timeout=15,
        )
        d2 = r2.json()
        ig_id = d2.get("instagram_business_account", {}).get("id", "")

        ig_username = ""
        if ig_id:
            r3 = requests.get(
                f"{GRAPH_BASE}/{ig_id}",
                params={"fields": "username", "access_token": pg_token},
                timeout=10,
            )
            ig_username = r3.json().get("username", "")

        print(f"  • Página: {pg_nome:40s} | IG: @{ig_username} (ID: {ig_id})")
        resultados.append({
            "pg_id":       pg_id,
            "pg_nome":     pg_nome,
            "pg_token":    pg_token,
            "ig_id":       ig_id,
            "ig_username": ig_username,
        })

    return resultados


def associar_contas(config: dict, paginas: list[dict], user_token: str):
    """
    Pergunta ao usuário qual página corresponde a cada conta do Instagram.
    """
    separador("PASSO 3 — Associar páginas às contas do Instagram")

    for nome_conta in CONTAS:
        print(f"\n  Qual página do Facebook corresponde a @{nome_conta}?")
        for i, pg in enumerate(paginas):
            ig = f"@{pg['ig_username']}" if pg["ig_username"] else "(sem IG vinculado)"
            print(f"    [{i}] {pg['pg_nome']} — Instagram: {ig}")
        print(f"    [s] Pular esta conta")

        escolha = input("  Escolha o número: ").strip().lower()
        if escolha == "s":
            print(f"  Pulando {nome_conta}.")
            continue

        try:
            idx = int(escolha)
            pg  = paginas[idx]
        except (ValueError, IndexError):
            print("  Opção inválida. Pulando.")
            continue

        if not pg["ig_id"]:
            print(f"  [AVISO] Página '{pg['pg_nome']}' não tem conta Instagram vinculada.")
            print("  Certifique-se que a conta Instagram está configurada como")
            print("  'Profissional' e vinculada a esta Página do Facebook.")
            continue

        # Salva token da página (não do usuário) — é o correto para publicar
        config["accounts"][nome_conta]["token"]      = pg["pg_token"]
        config["accounts"][nome_conta]["ig_user_id"] = pg["ig_id"]
        print(f"  ✓ {nome_conta} → @{pg['ig_username']} (ID: {pg['ig_id']})")


def configurar_imgbb(config: dict):
    """Permite configurar a chave do imgbb."""
    separador("PASSO 4 — Configurar imgbb (hospedagem de imagens)")

    atual = config.get("imgbb_api_key", "")
    if atual:
        print(f"  Chave atual: {atual[:8]}...")
        trocar = input("  Deseja alterar? (s/N): ").strip().lower()
        if trocar != "s":
            return

    print("""
  O script usa o imgbb para hospedar temporariamente as imagens
  antes de enviá-las ao Instagram (a API exige URL pública).

  Para obter sua chave gratuita:
    1. Acesse: https://imgbb.com/
    2. Crie uma conta gratuita
    3. Acesse: https://api.imgbb.com/
    4. Clique em "Get API key"
    5. Copie e cole abaixo
""")
    key = input("  Cole sua API key do imgbb (ou ENTER para pular): ").strip()
    if key:
        config["imgbb_api_key"] = key
        print("  ✓ Chave configurada.")


def verificar_tokens(config: dict):
    """Verifica quais tokens ainda são válidos."""
    separador("Verificando tokens existentes")

    for nome, conta in config["accounts"].items():
        token = conta.get("token", "")
        ig_id = conta.get("ig_user_id", "")

        if not token:
            print(f"  [{nome}] ✗ Sem token")
            continue

        r = requests.get(
            f"{GRAPH_BASE}/me",
            params={"access_token": token},
            timeout=10,
        )
        if r.status_code == 200:
            print(f"  [{nome}] ✓ Token válido | IG User ID: {ig_id or 'NÃO CONFIGURADO'}")
        else:
            err = r.json().get("error", {}).get("message", "desconhecido")
            print(f"  [{nome}] ✗ Token inválido ({err})")


def menu_principal():
    config = carregar_ou_criar_config()

    print("""
╔══════════════════════════════════════════════════════════╗
║       Token Helper — Espaço Laser Instagram              ║
╚══════════════════════════════════════════════════════════╝

  [1] Gerar tokens novos (fluxo completo — OAuth)
  [2] Verificar status dos tokens atuais
  [3] Configurar chave imgbb
  [4] Sair
""")

    opcao = input("  Escolha: ").strip()

    if opcao == "1":
        user_token = obter_token_via_oauth()
        if not user_token:
            print("\n[ERRO] Não foi possível obter token. Verifique e tente novamente.")
            return

        paginas = listar_paginas_e_contas_ig(user_token)
        if not paginas:
            print("\n[ERRO] Nenhuma página encontrada. Verifique as permissões do app.")
            return

        associar_contas(config, paginas, user_token)
        configurar_imgbb(config)
        salvar_config(config)

        separador("Concluído!")
        print("""
  Configuração salva em tokens.json.

  Próximos passos:
    1. Teste localmente: python instagram_poster.py
    2. No Railway, adicione as variáveis de ambiente:
       SISMAKER_LOGIN, SISMAKER_SENHA, IMGBB_API_KEY
       (os tokens ficam em tokens.json que deve estar no container)
""")

    elif opcao == "2":
        verificar_tokens(config)

    elif opcao == "3":
        configurar_imgbb(config)
        salvar_config(config)

    elif opcao == "4":
        print("  Saindo.")
    else:
        print("  Opção inválida.")


if __name__ == "__main__":
    menu_principal()
