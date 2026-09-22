"""
Automação: Sismaker → Instagram Poster
========================================
Fluxo:
1. Login no Sismaker (novo.sismaker.com)
2. Acessa a página de downloads (/download_infinity_pages/44767)
3. Verifica se há posts para hoje
4. Baixa as imagens encontradas (suporta post único ou carrossel)
5. Faz upload para host público (catbox.moe, fallback imgbb)
6. Posta no Instagram de cada unidade via Graph API

Contas gerenciadas:
- @espacolaser.uba
- @espacolaser.ibiuna
- @espacolaser.monlevade
- @espacolaser.patrociniomg
"""

import os
import re
import sys
import json
import time
import base64
import mimetypes
import zipfile
import requests
import tempfile
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright
import pytz

# Evita crash em console Windows (cp1252) ao imprimir emoji/acentos — em
# Linux/Docker (produção) já é utf-8 por padrão, então isso é só defensivo.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

# ─── Z-API (WhatsApp) ────────────────────────────────────────────────────────
# Sem valor padrão de propósito — precisam vir de variável de ambiente
# (Railway → Service → Variables). Se faltar alguma, enviar_whatsapp() só
# avisa e pula a notificação, não derruba o resto do script.
ZAPI_INSTANCE = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN = os.environ.get("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.environ.get("ZAPI_CLIENT_TOKEN", "")
ZAPI_PHONE = os.environ.get("ZAPI_PHONE", "")

# ─── Controle de estado (evita duplo disparo) ─────────────────────────────────
# Por padrão fica no diretório de trabalho (some a cada redeploy, container
# é efêmero). Em produção (Railway) aponte STATE_FILE pra um caminho dentro
# de um Volume (ex: /data/ig_poster_state.json) — assim o controle "já
# postou hoje" sobrevive a redeploys, evitando postar de novo se o serviço
# reiniciar entre os dois horários do cron.
STATE_FILE = os.environ.get("STATE_FILE", "ig_poster_state.json")

# ─── Configurações ────────────────────────────────────────────────────────────

SISMAKER_URL = "https://novo.sismaker.com/espacolaser/download_infinity_pages/44767"
# Sem valor padrão de propósito — configure SISMAKER_LOGIN/SISMAKER_SENHA como
# variável de ambiente (Railway → Service → Variables). main() verifica que
# as duas estão preenchidas antes de tentar logar.
SISMAKER_LOGIN = os.environ.get("SISMAKER_LOGIN", "")
SISMAKER_SENHA = os.environ.get("SISMAKER_SENHA", "")
TOKENS_FILE = os.environ.get("TOKENS_FILE", "tokens.json")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")
TZ_SP = pytz.timezone("America/Sao_Paulo")
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "sismaker_posts"

# Hospedagem própria da mídia (ver fileserver.py) — usada antes de tentar
# hosts de terceiros (catbox/imgbb), que se mostraram pouco confiáveis:
# throttling, e o fetcher do próprio Meta sendo barrado ao processar Feed/
# Reels/Story mesmo com o upload em si tendo funcionado (observado em
# 09-12/09/2026). PUBLIC_BASE_URL é o domínio público que o Railway gera
# pra este serviço (Settings → Networking → Generate Domain).
PUBLIC_UPLOAD_DIR = Path(os.environ.get("PUBLIC_UPLOAD_DIR", "/app/public_uploads"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Limite de itens por carrossel imposto pela Graph API do Instagram (o app
# nativo aceita até 20, mas a API de publicação continua limitada a 10 —
# confirmado testando as versões v19 a v23 em 08/09/2026).
MAX_CAROUSEL_ITEMS = 10

# Legenda padrão — personalize conforme necessário
# Legenda padrão — será sobrescrita pelo texto encontrado no Sismaker
CAPTION_PADRAO = os.environ.get(
    "IG_CAPTION",
    "✨ Remova pelos indesejados com segurança e eficiência!\n"
    "#EspaçoLaser #DepilacaoALaser #BeautyTech"
)

# Para testar com data diferente: defina DATA_ALVO=02/04 (formato dd/mm)
DATA_ALVO = os.environ.get("DATA_ALVO", "")

# ─── Controle de Estado ──────────────────────────────────────────────────────

def ja_postou_hoje() -> bool:
    """Retorna True se já houve postagem bem-sucedida hoje."""
    today = datetime.now(TZ_SP).strftime("%Y-%m-%d")
    if not Path(STATE_FILE).exists():
        return False
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    return state.get("data") == today and state.get("postado") is True

def marcar_postado(resumo: dict):
    """Salva estado de postagem do dia com resumo das contas."""
    today = datetime.now(TZ_SP).strftime("%Y-%m-%d")
    hora = datetime.now(TZ_SP).strftime("%H:%M")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"data": today, "hora": hora, "postado": True, "resumo": resumo}, f,
                   indent=2, ensure_ascii=False)

# ─── Notificação WhatsApp (Z-API) ─────────────────────────────────────────────

def enviar_whatsapp(mensagem: str):
    """Envia mensagem de texto via Z-API."""
    if not (ZAPI_INSTANCE and ZAPI_TOKEN and ZAPI_CLIENT_TOKEN and ZAPI_PHONE):
        print(" [AVISO] Z-API não configurado (ZAPI_INSTANCE/ZAPI_TOKEN/ZAPI_CLIENT_TOKEN/"
              "ZAPI_PHONE) — notificação pulada.")
        return
    url = (f"https://api.z-api.io/instances/{ZAPI_INSTANCE}"
           f"/token/{ZAPI_TOKEN}/send-text")
    try:
        r = requests.post(
            url,
            json={"phone": ZAPI_PHONE, "message": mensagem},
            headers={"Client-Token": ZAPI_CLIENT_TOKEN},
            timeout=15,
        )
        if r.status_code == 200:
            print(" WhatsApp enviado com sucesso!")
        else:
            print(f" [AVISO] WhatsApp status {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f" [AVISO] Falha ao enviar WhatsApp: {e}")

# ─── Gerenciamento de Tokens ──────────────────────────────────────────────────

def carregar_tokens() -> dict:
    """Carrega a config de tokens (app_id/app_secret/imgbb/contas).

    Ordem de preferência:
    1. Variável de ambiente TOKENS_JSON (o conteúdo inteiro do tokens.json,
       em uma linha só) — é assim que roda em produção no Railway, pra não
       precisar commitar tokens de verdade no repositório (que é público).
       Sempre que definida, é a fonte da verdade — e é regravada em
       TOKENS_FILE (útil só pra registrar renovação de token nos logs;
       NÃO é o que decide o que carregar na próxima vez, veja abaixo).
    2. Arquivo local TOKENS_FILE — só entra em jogo quando TOKENS_JSON não
       está definida (uso local, sem versionar — veja tokens.example.json).

    IMPORTANTE: TOKENS_JSON sempre tem prioridade sobre o arquivo, mesmo
    que o arquivo já exista de uma execução anterior. Antes disso, um
    TOKENS_FILE remanescente no Volume (gravado por uma run anterior)
    ficava "preso" e uma mudança em TOKENS_JSON no Railway (ex: reativar
    uma loja) nunca tinha efeito, porque o arquivo velho sempre ganhava —
    foi exatamente o que aconteceu em 12/09/2026 (reabilitação de 3 lojas
    não pegou porque o Volume já tinha um tokens.json de uma run anterior
    com elas desabilitadas).
    """
    tokens_json_env = os.environ.get("TOKENS_JSON")
    if tokens_json_env:
        config = json.loads(tokens_json_env)
        salvar_tokens(config)
        return config

    if Path(TOKENS_FILE).exists():
        with open(TOKENS_FILE, encoding="utf-8") as f:
            return json.load(f)

    raise FileNotFoundError(
        f"Nem a variável de ambiente TOKENS_JSON nem '{TOKENS_FILE}' foram encontrados.\n"
        "Configure TOKENS_JSON no Railway (Service → Variables, com o conteúdo do "
        "tokens.json numa linha só) ou rode localmente com um tokens.json "
        "(veja tokens.example.json) — ou execute: python token_helper.py"
    )

def salvar_tokens(data: dict):
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def verificar_e_renovar_token(loja: str, config: dict) -> str | None:
    """Verifica validade do token e tenta renovar se necessário."""
    conta = config["accounts"][loja]
    token = conta.get("token", "")
    if not token:
        print(f" [{loja}] Token não configurado. Execute token_helper.py.")
        return None

    auth_type = conta.get("auth_type", "facebook_login")

    # ── Instagram Login (nova API) ──────────────────────────────────────────
    if auth_type == "instagram_login":
        r = requests.get(
            "https://graph.instagram.com/v19.0/me",
            params={"access_token": token, "fields": "id,username"},
            timeout=10,
        )
        if r.status_code == 200:
            return token
        print(f" [{loja}] Token Instagram inválido, tentando renovar...")
        r2 = requests.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
            timeout=10,
        )
        data = r2.json()
        if "access_token" in data:
            novo = data["access_token"]
            config["accounts"][loja]["token"] = novo
            salvar_tokens(config)
            print(f" [{loja}] Token Instagram renovado.")
            return novo
        print(f" [{loja}] Não foi possível renovar token Instagram: {data.get('error', data)}")
        return None

    # ── Facebook Login (API clássica) ───────────────────────────────────────
    r = requests.get(
        "https://graph.facebook.com/v19.0/me",
        params={"access_token": token},
        timeout=10,
    )
    if r.status_code == 200:
        return token

    print(f" [{loja}] Token inválido (código {r.status_code}), tentando renovar...")
    r2 = requests.get(
        "https://graph.facebook.com/v19.0/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": config["app_id"],
            "client_secret": config["app_secret"],
            "fb_exchange_token": token,
        },
        timeout=10,
    )
    data = r2.json()
    if "access_token" in data:
        novo = data["access_token"]
        config["accounts"][loja]["token"] = novo
        salvar_tokens(config)
        print(f" [{loja}] Token renovado com sucesso.")
        return novo

    print(f" [{loja}] Não foi possível renovar: {data.get('error', data)}")
    print(f" [{loja}] Gere um novo token com: python token_helper.py")
    return None

# ─── Pré-processamento de Imagem ─────────────────────────────────────────────

def preparar_imagem(src: Path, prefixo: str) -> Path:
    """
    Converte a imagem para JPEG RGB sem canal alpha.
    Garante que o aspect ratio do feed esteja dentro do limite do Instagram (0.8 a 1.91).
    Retorna o caminho do arquivo preparado.
    """
    from PIL import Image

    img = Image.open(src).convert("RGB")
    w, h = img.size
    ratio = w / h

    # Instagram feed: ratio entre 0.8 (4:5) e 1.91 (quase 2:1)
    if "story" not in prefixo:
        if ratio < 0.79:
            # Muito alto — corta lateralmente para 4:5
            novo_w = int(h * 0.8)
            offset = (w - novo_w) // 2
            img = img.crop((offset, 0, offset + novo_w, h))
            print(f" Imagem {prefixo} recortada para 4:5 ({novo_w}x{h})")
        elif ratio > 1.92:
            # Muito largo — corta verticalmente para 1.91:1
            novo_h = int(w / 1.91)
            offset = (h - novo_h) // 2
            img = img.crop((0, offset, w, offset + novo_h))
            print(f" Imagem {prefixo} recortada para 1.91:1 ({w}x{novo_h})")

    dest = src.with_suffix(".jpg")
    img.save(dest, "JPEG", quality=95)
    return dest

# ─── Hospedagem própria (fileserver.py) ────────────────────────────────────────

def hospedar_localmente(caminho: Path) -> str | None:
    """Copia o arquivo pra pasta servida pelo fileserver.py deste mesmo
    container e devolve a URL pública (PUBLIC_BASE_URL + nome aleatório).
    Não depende de nenhum serviço de terceiros — é a opção preferida
    quando PUBLIC_BASE_URL está configurado (ver comentário na declaração
    da variável)."""
    if not PUBLIC_BASE_URL:
        return None
    try:
        import shutil
        import uuid
        PUBLIC_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        nome = f"{uuid.uuid4().hex}{caminho.suffix}"
        destino = PUBLIC_UPLOAD_DIR / nome
        shutil.copy(caminho, destino)
        url = f"{PUBLIC_BASE_URL}/{nome}"
        print(f" Hospedado no próprio servidor: {url}")
        return url
    except Exception as e:
        print(f" [AVISO] Falha ao hospedar localmente: {e}")
        return None

# ─── Upload de Imagem (imgbb) ─────────────────────────────────────────────────

def upload_imgbb(image_path: Path, api_key: str) -> str | None:
    """
    Faz upload da mídia (imagem OU vídeo) em host público para o Instagram
    conseguir baixar. Tenta primeiro a hospedagem própria deste container
    (hospedar_localmente — não depende de terceiros), depois catbox.moe
    (sem hotlink protection, e é o único dos dois hosts de terceiros que
    aceita vídeo); imgbb só entra como último fallback pra IMAGEM — imgbb
    não suporta vídeo, tentar lá sempre dá "Unsupported or
    unrecognized file format", então nem tenta nesse caso.
    """
    url_propria = hospedar_localmente(image_path)
    if url_propria:
        return url_propria

    extensoes_video = {".mp4", ".mov", ".avi", ".mkv"}
    is_video = image_path.suffix.lower() in extensoes_video
    content_type = mimetypes.guess_type(image_path.name)[0] or (
        "video/mp4" if is_video else "image/jpeg"
    )

    # Vídeo (Reels) varia muito de tamanho — já vimos de ~20MB a mais de
    # 120MB no mesmo mês. Um timeout fixo de 180s não é suficiente pros
    # maiores (um vídeo de 123MB levou quase 7min pra subir numa conexão
    # comum em 12/09/2026). Escala o timeout pelo tamanho do arquivo, com
    # um piso generoso e um teto pra não travar pra sempre numa rede ruim.
    if is_video:
        tamanho_mb = image_path.stat().st_size / (1024 * 1024)
        timeout_catbox = min(900, max(180, int(60 + tamanho_mb * 5)))
    else:
        timeout_catbox = 30
    tentativas_catbox = 2 if is_video else 1

    for tentativa in range(1, tentativas_catbox + 1):
        try:
            with open(image_path, "rb") as f:
                r = requests.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": (image_path.name, f, content_type)},
                    timeout=timeout_catbox,
                )
            if r.status_code == 200 and r.text.strip().startswith("https://"):
                url = r.text.strip()
                print(f" Upload OK (catbox.moe): {url}")
                return url
            print(f" [AVISO] catbox.moe retornou: {r.text[:100]}")
        except Exception as e:
            print(f" [AVISO] catbox.moe falhou (tentativa {tentativa}/{tentativas_catbox}): {e}")

    if is_video:
        print(" [ERRO] catbox.moe falhou pro vídeo — imgbb não suporta vídeo, sem host disponível.")
        return None

    # ── Fallback: imgbb (só imagem — não suporta vídeo) ────────────────────────
    if api_key:
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            r = requests.post(
                "https://api.imgbb.com/1/upload",
                data={"key": api_key, "image": img_b64, "expiration": 600},
                timeout=30,
            )
            data = r.json()
            if data.get("success"):
                url = data["data"]["url"]
                print(f" Upload OK (imgbb): {url}")
                return url
            print(f" [ERRO imgbb] {data}")
        except Exception as e:
            print(f" [AVISO] imgbb falhou: {e}")

    return None

# ─── Instagram Graph API ──────────────────────────────────────────────────────

def _aguardar_processamento(base: str, creation_id: str, token: str,
                             timeout_s: int = 180, intervalo: int = 10) -> bool:
    """Espera o container de mídia terminar de processar do lado do Meta antes
    de publicar. Essencial pra vídeo/Reels — o tempo de processamento varia
    (às vezes passa bem de 30s), e publicar antes de terminar dá o erro
    "Media ID is not available" (code 9007). Imagem processa quase na hora,
    mas consultar do mesmo jeito não tem custo.
    Retorna True se ficou pronto (FINISHED), False se deu erro ou estourou o tempo.
    """
    decorrido = 0
    while decorrido < timeout_s:
        try:
            r = requests.get(
                f"{base}/{creation_id}",
                params={"access_token": token, "fields": "status_code"},
                timeout=15,
            )
            status = r.json().get("status_code")
        except Exception as e:
            print(f" [AVISO] Falha ao consultar status do processamento: {e}")
            status = None
        if status == "FINISHED":
            return True
        if status == "ERROR":
            print(" [ERRO] Processamento da mídia falhou (status_code=ERROR).")
            return False
        time.sleep(intervalo)
        decorrido += intervalo
    print(f" [AVISO] Mídia não terminou de processar em {timeout_s}s.")
    return False

def postar_instagram(ig_user_id: str, token: str, image_url: str, caption: str,
                      is_story: bool = False, is_video: bool = False,
                      tentativas: int = 3, auth_type: str = "facebook_login") -> bool:
    """
    Publica feed (imagem ou vídeo/Reels) ou story no Instagram via Graph API.
    Suporta Facebook Login (graph.facebook.com) e Instagram Login (graph.instagram.com).
    Faz até `tentativas` retentativas em erros transitórios.
    """
    if auth_type == "instagram_login":
        base = "https://graph.instagram.com/v19.0"
    else:
        base = "https://graph.facebook.com/v19.0"
    if is_story:
        tipo = "Story"
    elif is_video:
        tipo = "Reels"
    else:
        tipo = "Feed"

    # Monta parâmetros conforme o tipo de mídia
    if is_video:
        api_params = {"access_token": token, "video_url": image_url, "media_type": "REELS"}
        data_body = {"caption": caption}
    elif is_story:
        api_params = {"access_token": token, "image_url": image_url, "media_type": "STORIES"}
        data_body = {}
    else:
        api_params = {"access_token": token, "image_url": image_url}
        data_body = {"caption": caption}

    for tentativa in range(1, tentativas + 1):
        if tentativa > 1:
            espera = tentativa * 10
            print(f" Tentativa {tentativa}/{tentativas} em {espera}s...")
            time.sleep(espera)

        # Passo 1 — criar container
        r1 = requests.post(
            f"{base}/{ig_user_id}/media",
            params=api_params,
            data=data_body if data_body else None,
            timeout=30,
        )
        d1 = r1.json()
        if "id" not in d1:
            err = d1.get("error", d1)
            is_transient = d1.get("error", {}).get("is_transient", False)
            print(f" [ERRO] Criar container {tipo}: {err}")
            if is_transient and tentativa < tentativas:
                continue
            return False

        creation_id = d1["id"]
        print(f" Container {tipo} criado: {creation_id}. Aguardando processamento...")
        pronto = _aguardar_processamento(base, creation_id, token,
                                          timeout_s=180 if is_video else 30)
        if not pronto:
            if tentativa < tentativas:
                continue
            return False

        # Passo 2 — publicar
        r2 = requests.post(
            f"{base}/{ig_user_id}/media_publish",
            params={"access_token": token},
            data={"creation_id": creation_id},
            timeout=30,
        )
        d2 = r2.json()
        if "id" in d2:
            print(f" {tipo} publicado! Post ID: {d2['id']}")
            return True

        err2 = d2.get("error", d2)
        print(f" [ERRO] Publicar {tipo}: {err2}")
        # code 9007 = "Media ID is not available" — a mídia ainda não estava
        # pronta apesar do status_code dizer FINISHED (raro, mas acontece).
        # is_transient cobre outros erros passageiros do lado do Meta.
        retentavel = err2.get("code") == 9007 or err2.get("is_transient", False)
        if retentavel and tentativa < tentativas:
            continue
        return False

    return False

def postar_carrossel_instagram(ig_user_id: str, token: str, image_urls: list, caption: str,
                                auth_type: str = "facebook_login", tentativas: int = 3) -> bool:
    """
    Publica um carrossel de feed (2 a MAX_CAROUSEL_ITEMS imagens) via Graph API:
    cria um container por imagem (is_carousel_item), depois um container "pai"
    (media_type=CAROUSEL, children=[...]), e por fim publica o pai.
    """
    if auth_type == "instagram_login":
        base = "https://graph.instagram.com/v19.0"
    else:
        base = "https://graph.facebook.com/v19.0"

    if len(image_urls) > MAX_CAROUSEL_ITEMS:
        print(f" [AVISO] Carrossel com {len(image_urls)} imagens excede o limite de "
              f"{MAX_CAROUSEL_ITEMS} da API — cortando para as primeiras {MAX_CAROUSEL_ITEMS}.")
        image_urls = image_urls[:MAX_CAROUSEL_ITEMS]

    item_ids = []
    for url in image_urls:
        r = requests.post(
            f"{base}/{ig_user_id}/media",
            params={"access_token": token, "image_url": url, "is_carousel_item": "true"},
            timeout=30,
        )
        d = r.json()
        if "id" not in d:
            print(f" [ERRO] Criar item do carrossel: {d.get('error', d)}")
            return False
        item_ids.append(d["id"])

    for tentativa in range(1, tentativas + 1):
        if tentativa > 1:
            espera = tentativa * 10
            print(f" Tentativa {tentativa}/{tentativas} em {espera}s...")
            time.sleep(espera)

        r1 = requests.post(
            f"{base}/{ig_user_id}/media",
            params={"access_token": token},
            data={"media_type": "CAROUSEL", "children": ",".join(item_ids), "caption": caption},
            timeout=30,
        )
        d1 = r1.json()
        if "id" not in d1:
            err = d1.get("error", d1)
            print(f" [ERRO] Criar container do carrossel: {err}")
            if d1.get("error", {}).get("is_transient", False) and tentativa < tentativas:
                continue
            return False

        creation_id = d1["id"]
        print(f" Container Carrossel criado: {creation_id}. Aguardando processamento...")
        pronto = _aguardar_processamento(base, creation_id, token, timeout_s=60)
        if not pronto:
            if tentativa < tentativas:
                continue
            return False

        r2 = requests.post(
            f"{base}/{ig_user_id}/media_publish",
            params={"access_token": token},
            data={"creation_id": creation_id},
            timeout=30,
        )
        d2 = r2.json()
        if "id" in d2:
            print(f" Carrossel publicado! Post ID: {d2['id']}")
            return True

        err2 = d2.get("error", d2)
        print(f" [ERRO] Publicar carrossel: {err2}")
        retentavel = err2.get("code") == 9007 or err2.get("is_transient", False)
        if retentavel and tentativa < tentativas:
            continue
        return False

    return False

# ─── Helpers de arquivo ──────────────────────────────────────────────────────

def _texto_de_pdf(pdf_bytes: bytes) -> str | None:
    """Extrai o texto de um PDF (usa pdfplumber se disponível). Compartilhado
    entre a legenda de dentro de um ZIP e um PDF baixado avulso (ver
    baixar_arquivo — o Sismaker às vezes manda a legenda como seu próprio
    botão "Baixar" separado da mídia, em vez de dentro do mesmo ZIP)."""
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            texto = "\n".join(pg.extract_text() or "" for pg in pdf.pages).strip()
        return texto if len(texto) > 5 else None
    except ImportError:
        print(" [AVISO] pdfplumber não instalado — legenda PDF ignorada")
        return None
    except Exception as e:
        print(f" [AVISO] Erro ao ler PDF: {e}")
        return None

def extrair_midias_do_zip(zip_path: Path, prefixo: str, data_iso: str) -> tuple:
    """Extrai as imagens (uma, várias — carrossel — ou uma combinação de
    Feed+Story) ou um vídeo, e tenta extrair legenda (TXT/PDF) de um ZIP.

    Quando há mais de uma imagem, elas são ordenadas pelo número final do nome
    do arquivo (ex: "3_04.jpg" → 04), que é a ordem real do carrossel no
    Sismaker — não pelo tamanho do arquivo, que não reflete posição nenhuma.

    Alguns ZIPs trazem a MESMA arte em dois formatos dentro do mesmo botão
    "Baixar" — ex: "0_1409_Feed.jpg" (4:5) + "2_1409_Story.jpg" (9:16),
    confirmado em 14/09/2026. Sem separar isso, as duas imagens viravam um
    carrossel de 2 itens (errado — nem é a mesma arte repetida à toa, é
    Feed e Story juntos). Por isso cada imagem é checada pelo nome: se tiver
    "story"/"feed" no nome, vira seu próprio grupo; do contrário entra no
    grupo "carrossel" (sem rótulo — caso normal de várias fotos numeradas).

    Retorna: (grupos, texto_legenda), onde grupos é uma lista de
    {"tipo": "feed" | "story" | None, "paths": [Path, ...]}.
    "tipo" None com mais de um path = carrossel de verdade (mesmo tipo,
    várias fotos). "tipo" "story"/"feed" = grupo isolado daquele formato,
    mesmo que só tenha 1 imagem.
    """
    extensoes_imagem = {".jpg", ".jpeg", ".png"}
    extensoes_video = {".mp4", ".mov", ".avi", ".mkv"}
    grupos: list[dict] = []
    caption_text = None
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            nomes = z.namelist()
            imagens = [n for n in nomes if Path(n).suffix.lower() in extensoes_imagem]
            videos = [n for n in nomes if Path(n).suffix.lower() in extensoes_video]
            textos = [n for n in nomes if Path(n).suffix.lower() == ".txt"]
            pdfs = [n for n in nomes if Path(n).suffix.lower() == ".pdf"]

            def ordem(nome: str) -> int:
                m = re.search(r"(\d+)(?=\.\w+$)", nome)
                return int(m.group(1)) if m else nomes.index(nome)

            if imagens:
                imagens.sort(key=ordem)
                extraidos = []  # (Path, tipo|None)
                for idx, nome_img in enumerate(imagens, 1):
                    ext = Path(nome_img).suffix
                    dest = DOWNLOAD_DIR / f"{prefixo}_{data_iso}_{idx:02d}{ext}"
                    dest.write_bytes(z.read(nome_img))
                    nome_lower = nome_img.lower()
                    if "story" in nome_lower:
                        tipo = "story"
                    elif "feed" in nome_lower:
                        tipo = "feed"
                    else:
                        tipo = None
                    extraidos.append((dest, tipo))

                sem_rotulo = [p for p, t in extraidos if t is None]
                if sem_rotulo:
                    grupos.append({"tipo": None, "paths": sem_rotulo})
                for p, t in extraidos:
                    if t is not None:
                        grupos.append({"tipo": t, "paths": [p]})

                if len(extraidos) == 1:
                    print(f" {prefixo.capitalize()} extraído do ZIP: {extraidos[0][0].name}")
                elif any(t is not None for _, t in extraidos):
                    resumo = ", ".join(f"{p.name} ({t or 'carrossel'})" for p, t in extraidos)
                    print(f" {len(extraidos)} imagens extraídas do ZIP (formatos separados): {resumo}")
                else:
                    print(f" {len(extraidos)} imagens extraídas do ZIP (carrossel), prefixo '{prefixo}'")
            elif videos:
                # Vídeo como fallback se não tiver imagem
                videos.sort(key=ordem)
                nome_vid = videos[0]
                ext = Path(nome_vid).suffix
                dest = DOWNLOAD_DIR / f"{prefixo}_{data_iso}{ext}"
                dest.write_bytes(z.read(nome_vid))
                grupos.append({"tipo": None, "paths": [dest]})
                print(f" Vídeo extraído do ZIP: {dest.name}")
            else:
                print(f" [AVISO] ZIP sem imagens ou vídeos: {zip_path.name}")

            # Tenta extrair legenda de TXT
            if textos:
                try:
                    txt = z.read(textos[0]).decode("utf-8", errors="ignore").strip()
                    if len(txt) > 5:
                        caption_text = txt
                        print(f" Legenda TXT encontrada no ZIP ({len(txt)} chars)")
                except Exception as e:
                    print(f" [AVISO] Erro ao ler TXT do ZIP: {e}")

            # Tenta extrair legenda de PDF (usa pdfplumber se disponível)
            if not caption_text and pdfs:
                texto_pdf = _texto_de_pdf(z.read(pdfs[0]))
                if texto_pdf:
                    caption_text = texto_pdf
                    print(f" Legenda PDF encontrada no ZIP ({len(texto_pdf)} chars)")

    except Exception as e:
        print(f" [AVISO] Erro ao extrair ZIP {zip_path.name}: {e}")
    return grupos, caption_text

# ─── Scraping do Sismaker ─────────────────────────────────────────────────────

def buscar_posts(data_alvo: str = "") -> dict:
    """
    Faz login no Sismaker, clica no card da data, baixa Feed e Story.

    data_alvo: "dd/mm" para forçar uma data específica (ex: "02/04").
    Se vazio, usa a data de hoje.

    Retorna:
    {
        "feeds": [{"paths": [Path, ...], "caption": str, "is_video": bool, "is_carousel": bool}, ...],
        "stories": [{"paths": [Path], "caption": str}, ...],
        "avisos": [str, ...],  # ver comentário na classificação, mais abaixo
    }
    """
    hoje_dt = datetime.now(TZ_SP)

    if data_alvo:
        # Permite "02/04" ou "02/04/2026"
        partes = data_alvo.split("/")
        dia, mes = partes[0], partes[1]
        ano = partes[2] if len(partes) == 3 else str(hoje_dt.year)
        data_curta = f"{dia}/{mes}"  # "02/04"
        data_longa = f"{dia}/{mes}/{ano}"  # "02/04/2026"
        data_iso = f"{ano}-{mes}-{dia}"  # "2026-04-02"
    else:
        data_curta = hoje_dt.strftime("%d/%m")  # "03/04"
        data_longa = hoje_dt.strftime("%d/%m/%Y")  # "03/04/2026"
        data_iso = hoje_dt.strftime("%Y-%m-%d")  # "2026-04-03"

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    resultado = {"feeds": [], "stories": [], "avisos": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        page = ctx.new_page()

        try:
            # ── 1. Login ──────────────────────────────────────────────────
            print(f" Abrindo Sismaker...")
            page.goto(
                "https://novo.sismaker.com/espacolaser/users/sign_in",
                wait_until="networkidle",
                timeout=30000,
            )
            for sel in ['input[name="user[email]"]', 'input[type="email"]']:
                try:
                    page.locator(sel).first.fill(SISMAKER_LOGIN, timeout=3000)
                    break
                except:
                    pass
            for sel in ['input[name="user[password]"]', 'input[type="password"]']:
                try:
                    page.locator(sel).first.fill(SISMAKER_SENHA, timeout=3000)
                    break
                except:
                    pass
            page.locator('input[type="submit"], button[type="submit"]').first.click()
            page.wait_for_load_state("networkidle", timeout=15000)
            print(f" Login realizado.")

            # ── 2. Página de cards (ano → mês → data) ──────────────────────
            page.goto(SISMAKER_URL, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            page.screenshot(path="debug_sismaker_cards.png")

            # ── 2.0 Clica no card do ano — nível que o Sismaker adicionou
            # depois que este script foi escrito originalmente (confirmado
            # em 08/09/2026: a página de "REDES SOCIAIS" agora mostra um
            # card por ano antes dos meses). ──────────────────────────────
            ano_loc = page.get_by_text(str(hoje_dt.year), exact=False)
            if ano_loc.count() > 0:
                print(f" Card do ano encontrado: '{hoje_dt.year}'")
                ano_loc.first.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(1500)
                page.screenshot(path="debug_sismaker_ano.png")

            # ── 2.1 Clica no card do mês (nível intermediário) ────────────
            MESES_PT = {
                1: "JANEIRO", 2: "FEVEREIRO", 3: "MARÇO", 4: "ABRIL",
                5: "MAIO", 6: "JUNHO", 7: "JULHO", 8: "AGOSTO",
                9: "SETEMBRO",10: "OUTUBRO", 11: "NOVEMBRO", 12: "DEZEMBRO"
            }
            if data_alvo:
                mes_num = int(data_alvo.split("/")[1])
            else:
                mes_num = hoje_dt.month
            nome_mes = MESES_PT[mes_num]

            # Tenta encontrar o card do mês com match parcial (o card tem ícone + texto)
            mes_loc = page.get_by_text(nome_mes, exact=False)
            if mes_loc.count() == 0:
                # Fallback: procura qualquer elemento clicável contendo o nome do mês
                mes_loc = page.locator(f"text={nome_mes}")
            if mes_loc.count() > 0:
                print(f" Card do mês encontrado: '{nome_mes}'")
                mes_loc.first.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(1500)
                page.screenshot(path="debug_sismaker_mes.png")
            else:
                print(f" Card do mês '{nome_mes}' não encontrado — buscando data diretamente")

            # ── 2.2 Procura o card da data ────────────────────────────────
            card = None
            for texto in [data_longa, data_curta]:
                loc = page.get_by_text(texto, exact=True)
                if loc.count() > 0:
                    card = loc.first
                    print(f" Card encontrado: '{texto}'")
                    break

            if card is None:
                print(f" Nenhum card para {data_longa} ({data_curta}) encontrado.")
                print(f" Verifique debug_sismaker_cards.png.")
                return resultado

            # ── 3. Entrar no card ─────────────────────────────────────────
            card.click()
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(1500)
            page.screenshot(path="debug_sismaker_conteudo.png")
            print(f" Página de conteúdo aberta.")

            # ── 4. Extrair legenda ─────────────────────────────────────────
            # Tenta encontrar texto de legenda na página (textarea, div específico)
            for sel_caption in [
                'textarea',
                '[class*="caption"]',
                '[class*="legenda"]',
                '[class*="texto"]',
                '[id*="caption"]',
                '[id*="legenda"]',
            ]:
                try:
                    el = page.locator(sel_caption).first
                    txt = el.inner_text(timeout=2000).strip()
                    if txt and len(txt) > 10:
                        resultado["caption"] = txt
                        print(f" Legenda encontrada: {txt[:60]}...")
                        break
                except:
                    pass

            # ── 5. Baixar todos os arquivos disponíveis ────────────────────
            secoes = page.locator('text="Baixar"')
            qtd = secoes.count()
            print(f" {qtd} botão(ões) 'Baixar' encontrado(s).")

            def rotulo_botao(idx: int) -> str:
                """Lê o rótulo que o próprio Sismaker mostra acima do botão
                (ex: "REELS: ...\\nreels - 1080x1920px", "STORY: ...\\nstory -
                ...", "FEED: ...\\npost feed - ..."). É a forma confiável de
                saber se é Feed, Story ou Reels — aspect ratio sozinho NÃO dá
                pra usar aqui porque Reels e Story são os dois 9:16.
                """
                try:
                    ancestor = secoes.nth(idx).locator("xpath=ancestor::*[2]")
                    return ancestor.inner_text(timeout=1000).strip().lower()
                except Exception:
                    return ""

            def baixar_arquivo(idx: int, label: str) -> tuple:
                """Clica em Baixar, salva e extrai ZIP se necessário.
                Retorna: (lista_de_grupos, str|None) — cada grupo é
                {"tipo": "feed"|"story"|None, "paths": [Path, ...]}.
                """
                try:
                    with page.expect_download(timeout=25000) as dl_info:
                        secoes.nth(idx).click()
                    dl = dl_info.value
                    dest_raw = DOWNLOAD_DIR / f"raw_{label}_{data_iso}{Path(dl.suggested_filename).suffix}"
                    dl.save_as(str(dest_raw))
                    if dest_raw.suffix.lower() == ".zip":
                        return extrair_midias_do_zip(dest_raw, label, data_iso)
                    if dest_raw.suffix.lower() == ".pdf":
                        # Botão "Baixar" próprio só pra legenda, sem mídia
                        # nenhuma (confirmado em 22/09/2026 — vídeo e legenda
                        # vieram em botões separados, não dentro do mesmo
                        # ZIP). Nenhum grupo de mídia, só a legenda em si.
                        texto = _texto_de_pdf(dest_raw.read_bytes())
                        if texto:
                            print(f" Legenda PDF avulsa encontrada: {dest_raw.name} ({len(texto)} chars)")
                        return [], texto
                    return [{"tipo": None, "paths": [dest_raw]}], None
                except Exception as e:
                    print(f" [AVISO] Download {label}: {e}")
                    return [], None

            arquivos_baixados = []  # lista de (lista_de_grupos, caption_str, rotulo_botao)
            legendas_avulsas = []  # PDFs de legenda baixados em botão próprio, sem mídia junto
            for i in range(qtd):  # baixa TODOS os botões disponíveis
                rotulo = rotulo_botao(i)
                grupos, cap = baixar_arquivo(i, str(i))
                if grupos:
                    arquivos_baixados.append((grupos, cap or CAPTION_PADRAO, rotulo))
                elif cap:
                    legendas_avulsas.append(cap)
                page.wait_for_timeout(800)

            # Um botão "Baixar" só de legenda (PDF avulso, sem mídia) serve
            # pra alguma outra mídia do mesmo card — aplica na primeira que
            # ainda estiver com a legenda genérica (sinal de que não achou
            # legenda própria). Não é garantido casar 100% certo quando há
            # mais de uma mídia sem legenda, mas resolve o caso comum
            # (1 mídia + 1 legenda avulsa) confirmado em 22/09/2026.
            for legenda in legendas_avulsas:
                for idx, (grupos, cap, rotulo) in enumerate(arquivos_baixados):
                    if cap == CAPTION_PADRAO:
                        arquivos_baixados[idx] = (grupos, legenda, rotulo)
                        print(f" Legenda avulsa aplicada a um dos itens baixados.")
                        break

            # ── 6. Classificar feed vs story ────────────────────────────────
            # Prioridade: 1) o rótulo dentro do nome do arquivo no ZIP
            # ("feed"/"story" — usado quando o mesmo ZIP traz a arte nos dois
            # formatos, ver extrair_midias_do_zip); 2) o rótulo do Sismaker
            # ("story"/"reels"/"feed") acima do botão; 3) aspect ratio (Story
            # ≈ 9:16, ratio < 0.7) como rede de segurança quando nada mais
            # deu pra ler.
            from PIL import Image as _PIL
            extensoes_video = {".mp4", ".mov", ".avi", ".mkv"}
            for grupos, cap, rotulo_btn in arquivos_baixados:
                eh_story_rotulo_btn = "story" in rotulo_btn
                eh_reels_rotulo_btn = "reels" in rotulo_btn
                eh_feed_rotulo_btn = "feed" in rotulo_btn

                for grupo in grupos:
                    paths = grupo["paths"]
                    tipo_arquivo = grupo["tipo"]  # "feed"/"story"/None, vindo do nome do arquivo

                    if len(paths) > 1:
                        # Mais de uma imagem no grupo — só é carrossel de Feed
                        # de verdade quando NADA (nem o nome dos arquivos, nem
                        # o rótulo do botão "Baixar") diz que é Story. Um
                        # botão "STORIES: ..." com várias imagens dentro (ex:
                        # 15/09/2026) NÃO é carrossel — Instagram não tem
                        # carrossel de story via API — é uma imagem por
                        # story, postadas em sequência.
                        if tipo_arquivo == "story" or (tipo_arquivo is None and eh_story_rotulo_btn):
                            for p in paths:
                                resultado["stories"].append({"paths": [p], "caption": cap})
                            print(f" → Story ({len(paths)} imagens em sequência, rótulo "
                                  f"{'no nome do arquivo' if tipo_arquivo else 'Sismaker'}) | legenda: {cap[:40]}...")
                            continue

                        resultado["feeds"].append({
                            "paths": paths, "caption": cap, "is_video": False, "is_carousel": True,
                        })
                        print(f" → Feed (carrossel, {len(paths)} imagens) | legenda: {cap[:40]}...")
                        continue

                    arq = paths[0]
                    try:
                        is_video = arq.suffix.lower() in extensoes_video

                        if tipo_arquivo == "story":
                            resultado["stories"].append({"paths": [arq], "caption": cap})
                            print(f" → Story (rótulo no nome do arquivo): {arq.name} | legenda: {cap[:40]}...")
                            continue

                        if tipo_arquivo == "feed":
                            resultado["feeds"].append({
                                "paths": [arq], "caption": cap, "is_video": is_video, "is_carousel": False,
                            })
                            print(f" → Feed (rótulo no nome do arquivo): {arq.name} | legenda: {cap[:40]}...")
                            continue

                        if eh_story_rotulo_btn:
                            resultado["stories"].append({"paths": [arq], "caption": cap})
                            print(f" → Story ({'vídeo' if is_video else 'imagem'}, rótulo Sismaker): "
                                  f"{arq.name} | legenda: {cap[:40]}...")
                            continue

                        if is_video or eh_reels_rotulo_btn or eh_feed_rotulo_btn:
                            resultado["feeds"].append({
                                "paths": [arq], "caption": cap, "is_video": is_video, "is_carousel": False,
                            })
                            print(f" → Feed ({'vídeo' if is_video else 'imagem'}, rótulo Sismaker): "
                                  f"{arq.name} | legenda: {cap[:40]}...")
                            continue

                        # Sem rótulo legível em nenhum nível (Sismaker mudou o
                        # layout?) — cai pro aspect ratio como antes, só pra
                        # imagem (vídeo sem rótulo vai pro Feed por padrão).
                        # Isso é um CHUTE, não uma leitura confiável — regra
                        # criada em 15/09/2026 depois de 3 dias seguidos com
                        # Feed/Story trocados: sempre que a classificação cai
                        # aqui, registra um aviso que vai junto na notificação
                        # do WhatsApp, pra alguém conferir aquele post à mão.
                        img = _PIL.open(arq)
                        ratio = img.width / img.height
                        img.close()
                        if ratio < 0.7:
                            resultado["stories"].append({"paths": [arq], "caption": cap})
                            print(f" → Story ({ratio:.2f}, sem rótulo): {arq.name} | legenda: {cap[:40]}...")
                        else:
                            resultado["feeds"].append({
                                "paths": [arq], "caption": cap, "is_video": False, "is_carousel": False,
                            })
                            print(f" → Feed ({ratio:.2f}, sem rótulo): {arq.name} | legenda: {cap[:40]}...")
                        resultado["avisos"].append(
                            f"{arq.name}: classificado só pela proporção da imagem "
                            f"({ratio:.2f}) — Sismaker não deu nenhum rótulo Feed/Story pra "
                            f"esse arquivo. Confira se foi pro lugar certo."
                        )
                    except Exception as e:
                        print(f" [AVISO] Não foi possível classificar {arq.name}: {e}")
                        resultado["avisos"].append(f"{arq.name}: falha ao classificar ({e}) — confira manualmente.")

        except Exception as e:
            print(f" [ERRO] Sismaker: {e}")
            try:
                page.screenshot(path="debug_sismaker_erro.png")
            except:
                pass
        finally:
            browser.close()

    return resultado

# ─── Fluxo Principal ──────────────────────────────────────────────────────────

def main():
    agora = datetime.now(TZ_SP)
    print("=" * 60)
    print(f" Instagram Poster — {agora.strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    if not SISMAKER_LOGIN or not SISMAKER_SENHA:
        print("\n[ERRO] Configure SISMAKER_LOGIN e SISMAKER_SENHA como variável de ambiente.")
        return

    # 0. Verificar se já postou hoje (evita duplo disparo 10h→18h)
    if not DATA_ALVO and ja_postou_hoje():
        print("\n Posts de hoje já foram publicados anteriormente. Encerrando.")
        return

    # 1. Carregar configuração de tokens
    try:
        config = carregar_tokens()
    except FileNotFoundError as e:
        print(f"\n[ERRO] {e}")
        return

    imgbb_key = config.get("imgbb_api_key") or IMGBB_API_KEY
    if not imgbb_key:
        print("\n[ERRO] Chave do imgbb não configurada.")
        return

    # 2. Buscar posts no Sismaker
    data_busca = DATA_ALVO  # "" = hoje, "02/04" = data específica
    label = f"data {data_busca}" if data_busca else "hoje"
    print(f"\n[1/3] Buscando posts de {label} no Sismaker...")
    posts = buscar_posts(data_busca)

    feeds = posts["feeds"]  # lista de {"paths": [...], "caption": str, "is_video": bool, "is_carousel": bool}
    stories = posts["stories"]  # lista de {"paths": [...], "caption": str}
    avisos_classificacao = posts.get("avisos", [])

    if not feeds and not stories:
        print(f"\n Nenhum arquivo baixado para {label}. Encerrando.")
        return

    print(f"\n Feeds encontrados: {len(feeds)}")
    print(f" Stories encontrados: {len(stories)}")

    # 3. Preparar imagens e fazer upload para URL pública
    print("\n[2/3] Preparando e fazendo upload das imagens...")
    feed_posts = []  # {"urls": [...], "caption": str, "is_video": bool, "is_carousel": bool}
    for i, item in enumerate(feeds):
        is_video = item.get("is_video", False)
        is_carousel = item.get("is_carousel", False)

        if is_video:
            url = upload_imgbb(item["paths"][0], imgbb_key)
            if url:
                feed_posts.append({"urls": [url], "caption": item["caption"], "is_video": True, "is_carousel": False})
                print(f" Vídeo {i+1} URL ok | legenda: {item['caption'][:50]}...")
            continue

        urls = []
        for j, p in enumerate(item["paths"]):
            prep = preparar_imagem(p, f"feed_{i}_{j}")
            url = upload_imgbb(prep, imgbb_key)
            if url:
                urls.append(url)
            else:
                print(f" [AVISO] Falha no upload de {p.name} — imagem removida do post")

        if not urls:
            continue

        if is_carousel and len(urls) > 1:
            feed_posts.append({"urls": urls, "caption": item["caption"], "is_video": False, "is_carousel": True})
            print(f" Carrossel {i+1} ({len(urls)} imagens) pronto | legenda: {item['caption'][:50]}...")
        else:
            feed_posts.append({"urls": [urls[0]], "caption": item["caption"], "is_video": False, "is_carousel": False})
            print(f" Feed {i+1} URL ok | legenda: {item['caption'][:50]}...")

    story_posts = []  # (url, caption)
    for i, item in enumerate(stories):
        url = upload_imgbb(preparar_imagem(item["paths"][0], f"story_{i}"), imgbb_key)
        if url:
            story_posts.append((url, item["caption"]))
            print(f" Story {i+1} URL ok | legenda: {item['caption'][:50]}...")

    if not feed_posts and not story_posts:
        print(" Falha no upload de todas as imagens. Encerrando.")
        return

    # 4. Postar em cada conta
    print("\n[3/3] Postando no Instagram...")
    resultados = {}

    for loja, conta in config["accounts"].items():
        if conta.get("disabled"):
            motivo = conta.get("disabled_reason", "desabilitado manualmente")
            print(f"\n [{loja}] DESABILITADO — {motivo[:80]}")
            resultados[loja] = "DESABILITADO"
            continue

        ig_user_id = conta.get("ig_user_id", "")
        if not ig_user_id:
            print(f"\n [{loja}] Sem ig_user_id — pulando.")
            resultados[loja] = "IGNORADO (sem ig_user_id)"
            continue

        token = verificar_e_renovar_token(loja, config)
        if not token:
            resultados[loja] = "FALHA (token inválido)"
            continue

        auth_type = conta.get("auth_type", "facebook_login")
        print(f"\n [{loja}] (auth: {auth_type})")
        ok_feeds = []
        ok_stories = []

        for post in feed_posts:
            if post["is_carousel"]:
                ok = postar_carrossel_instagram(ig_user_id, token, post["urls"], post["caption"], auth_type=auth_type)
            else:
                ok = postar_instagram(ig_user_id, token, post["urls"][0], post["caption"],
                                       is_story=False, is_video=post["is_video"], auth_type=auth_type)
            ok_feeds.append(ok)
            time.sleep(3)

        for story_url, cap in story_posts:
            ok = postar_instagram(ig_user_id, token, story_url, cap, is_story=True, auth_type=auth_type)
            ok_stories.append(ok)
            time.sleep(3)

        partes = []
        if feed_posts:
            partes.append(f"Feeds={sum(ok_feeds)}/{len(ok_feeds)} OK")
        if story_posts:
            partes.append(f"Stories={sum(ok_stories)}/{len(ok_stories)} OK")
        resultados[loja] = " | ".join(partes)

    # Resumo final
    print("\n" + "=" * 60)
    print(" RESUMO:")
    for loja, status in resultados.items():
        print(f" {loja:35s} → {status}")
    print("=" * 60)

    # Verifica se houve pelo menos 1 sucesso real (ex: "2/3 OK" conta; "0/1 OK" NÃO conta)
    houve_sucesso = any(
        bool(re.search(r'[1-9]\d*/\d+\s*OK', str(s)))
        for s in resultados.values()
    )

    if houve_sucesso and not DATA_ALVO:
        # Salva estado para evitar reexecução às 18h
        marcar_postado(resultados)

    # Monta mensagem WhatsApp
    hora = agora.strftime("%H:%M")
    data = agora.strftime("%d/%m/%Y")
    linhas = [f"✅ *Posts do Instagram publicados!*",
              f"📅 {data} às {hora}\n"]
    for loja, status in resultados.items():
        icone = "✓" if "OK" in str(status) else "✗"
        linhas.append(f" {icone} @{loja}: {status}")

    if avisos_classificacao:
        linhas.append("\n⚠️ *Confira estes posts* (não deu pra ter certeza se era Feed ou Story):")
        for aviso in avisos_classificacao:
            linhas.append(f" • {aviso}")

    linhas.append("\n_Enviado automaticamente pelo bot Espaço Laser_")

    print("\n Enviando notificação WhatsApp...")
    enviar_whatsapp("\n".join(linhas))

if __name__ == "__main__":
    main()
