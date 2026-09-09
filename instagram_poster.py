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
    1. Arquivo local TOKENS_FILE (útil pra rodar local, sem versionar — veja
       tokens.example.json pro formato).
    2. Variável de ambiente TOKENS_JSON (o conteúdo inteiro do tokens.json,
       em uma linha só) — é assim que roda em produção no Railway, pra não
       precisar commitar tokens de verdade no repositório (que é público).
       Quando carregado dessa forma, é gravado em TOKENS_FILE pra permitir
       que salvar_tokens() persista renovações de token durante a vida do
       container (não sobrevive a um redeploy — nesse caso volta a ler do
       TOKENS_JSON original).
    """
    if Path(TOKENS_FILE).exists():
        with open(TOKENS_FILE, encoding="utf-8") as f:
            return json.load(f)

    tokens_json_env = os.environ.get("TOKENS_JSON")
    if tokens_json_env:
        config = json.loads(tokens_json_env)
        salvar_tokens(config)
        return config

    raise FileNotFoundError(
        f"Nem '{TOKENS_FILE}' nem a variável de ambiente TOKENS_JSON foram encontrados.\n"
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

# ─── Upload de Imagem (imgbb) ─────────────────────────────────────────────────

def upload_imgbb(image_path: Path, api_key: str) -> str | None:
    """
    Faz upload da imagem em host público para o Instagram conseguir baixar.
    Tenta catbox.moe primeiro (sem hotlink protection), depois imgbb como fallback.
    """
    # ── Tentativa 1: catbox.moe (sem API key, sem hotlink protection) ─────────
    try:
        with open(image_path, "rb") as f:
            r = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (image_path.name, f, "image/jpeg")},
                timeout=30,
            )
        if r.status_code == 200 and r.text.strip().startswith("https://"):
            url = r.text.strip()
            print(f" Upload OK (catbox.moe): {url}")
            return url
        print(f" [AVISO] catbox.moe retornou: {r.text[:100]}")
    except Exception as e:
        print(f" [AVISO] catbox.moe falhou: {e}")

    # ── Tentativa 2: imgbb (com API key) ──────────────────────────────────────
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

def extrair_midias_do_zip(zip_path: Path, prefixo: str, data_iso: str) -> tuple:
    """Extrai as imagens (uma ou várias — carrossel) ou um vídeo, e tenta extrair
    legenda (TXT/PDF) de um ZIP.

    Quando há mais de uma imagem, elas são ordenadas pelo número final do nome
    do arquivo (ex: "3_04.jpg" → 04), que é a ordem real do carrossel no
    Sismaker — não pelo tamanho do arquivo, que não reflete posição nenhuma.

    Retorna: (lista_de_paths, texto_legenda)
    """
    extensoes_imagem = {".jpg", ".jpeg", ".png"}
    extensoes_video = {".mp4", ".mov", ".avi", ".mkv"}
    paths: list[Path] = []
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
                for idx, nome_img in enumerate(imagens, 1):
                    ext = Path(nome_img).suffix
                    dest = DOWNLOAD_DIR / f"{prefixo}_{data_iso}_{idx:02d}{ext}"
                    dest.write_bytes(z.read(nome_img))
                    paths.append(dest)
                if len(paths) == 1:
                    print(f" {prefixo.capitalize()} extraído do ZIP: {paths[0].name}")
                else:
                    print(f" {len(paths)} imagens extraídas do ZIP (carrossel), prefixo '{prefixo}'")
            elif videos:
                # Vídeo como fallback se não tiver imagem
                videos.sort(key=ordem)
                nome_vid = videos[0]
                ext = Path(nome_vid).suffix
                dest = DOWNLOAD_DIR / f"{prefixo}_{data_iso}{ext}"
                dest.write_bytes(z.read(nome_vid))
                paths.append(dest)
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
                try:
                    import io
                    import pdfplumber
                    pdf_bytes = z.read(pdfs[0])
                    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                        texto_pdf = "\n".join(
                            pg.extract_text() or "" for pg in pdf.pages
                        ).strip()
                    if len(texto_pdf) > 5:
                        caption_text = texto_pdf
                        print(f" Legenda PDF encontrada no ZIP ({len(texto_pdf)} chars)")
                except ImportError:
                    print(f" [AVISO] pdfplumber não instalado — legenda PDF ignorada")
                except Exception as e:
                    print(f" [AVISO] Erro ao ler PDF do ZIP: {e}")

    except Exception as e:
        print(f" [AVISO] Erro ao extrair ZIP {zip_path.name}: {e}")
    return paths, caption_text

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
    resultado = {"feeds": [], "stories": []}

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

            def baixar_arquivo(idx: int, label: str) -> tuple:
                """Clica em Baixar, salva e extrai ZIP se necessário.
                Retorna: (lista_de_paths, str|None)
                """
                try:
                    with page.expect_download(timeout=25000) as dl_info:
                        secoes.nth(idx).click()
                    dl = dl_info.value
                    dest_raw = DOWNLOAD_DIR / f"raw_{label}_{data_iso}{Path(dl.suggested_filename).suffix}"
                    dl.save_as(str(dest_raw))
                    if dest_raw.suffix.lower() == ".zip":
                        return extrair_midias_do_zip(dest_raw, label, data_iso)
                    return [dest_raw], None
                except Exception as e:
                    print(f" [AVISO] Download {label}: {e}")
                    return [], None

            arquivos_baixados = []  # lista de (lista_paths, caption_str)
            for i in range(qtd):  # baixa TODOS os botões disponíveis
                paths, cap = baixar_arquivo(i, str(i))
                if paths:
                    arquivos_baixados.append((paths, cap or CAPTION_PADRAO))
                page.wait_for_timeout(800)

            # ── 6. Classificar feed vs story ────────────────────────────────
            # Mais de 1 imagem = carrossel (Instagram não tem story em
            # carrossel via API, então isso é sempre Feed). Uma imagem só
            # classifica por aspect ratio, igual antes:
            # Story ≈ 9:16 (ratio < 0.7) | Feed ≈ 4:5+ | Vídeo → Feed (Reels).
            from PIL import Image as _PIL
            extensoes_video = {".mp4", ".mov", ".avi", ".mkv"}
            for paths, cap in arquivos_baixados:
                if len(paths) > 1:
                    resultado["feeds"].append({
                        "paths": paths, "caption": cap, "is_video": False, "is_carousel": True,
                    })
                    print(f" → Feed (carrossel, {len(paths)} imagens) | legenda: {cap[:40]}...")
                    continue

                arq = paths[0]
                try:
                    if arq.suffix.lower() in extensoes_video:
                        resultado["feeds"].append({
                            "paths": [arq], "caption": cap, "is_video": True, "is_carousel": False,
                        })
                        print(f" → Feed (vídeo): {arq.name} | legenda: {cap[:40]}...")
                        continue
                    img = _PIL.open(arq)
                    ratio = img.width / img.height
                    img.close()
                    if ratio < 0.7:
                        resultado["stories"].append({"paths": [arq], "caption": cap})
                        print(f" → Story: {arq.name} ({ratio:.2f}) | legenda: {cap[:40]}...")
                    else:
                        resultado["feeds"].append({
                            "paths": [arq], "caption": cap, "is_video": False, "is_carousel": False,
                        })
                        print(f" → Feed: {arq.name} ({ratio:.2f}) | legenda: {cap[:40]}...")
                except Exception as e:
                    print(f" [AVISO] Não foi possível classificar {arq.name}: {e}")

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
    linhas.append("\n_Enviado automaticamente pelo bot Espaço Laser_")

    print("\n Enviando notificação WhatsApp...")
    enviar_whatsapp("\n".join(linhas))

if __name__ == "__main__":
    main()
