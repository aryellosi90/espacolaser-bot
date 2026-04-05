"""
Automação: Sismaker → Instagram Poster
========================================
Fluxo:
  1. Login no Sismaker (novo.sismaker.com)
  2. Acessa a página de downloads (/download_infinity_pages/44767)
  3. Verifica se há posts para hoje
  4. Baixa as imagens encontradas
  5. Faz upload para imgbb (URL pública temporária)
  6. Posta no Instagram de cada unidade via Graph API

Contas gerenciadas:
  - @espacolaser.uba
  - @espacolaser.ibiuna
  - @espacolaser.monlevade
  - @espacolaser.patrociniomg
"""

import os
import re
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

# ─── Z-API (WhatsApp) ────────────────────────────────────────────────────────
ZAPI_INSTANCE      = os.environ.get("ZAPI_INSTANCE",      "3EB20541D8D291DF679CBE1FFBCB878E")
ZAPI_TOKEN         = os.environ.get("ZAPI_TOKEN",         "C3ACF20485B471C0AF93AE1A")
ZAPI_CLIENT_TOKEN  = os.environ.get("ZAPI_CLIENT_TOKEN",  "F36133724cab54a22b90414da356600faS")
ZAPI_PHONE         = os.environ.get("ZAPI_PHONE",         "5511964115060")

# ─── Controle de estado (evita duplo disparo) ─────────────────────────────────
STATE_FILE = "ig_poster_state.json"

# ─── Configurações ────────────────────────────────────────────────────────────

SISMAKER_URL   = "https://novo.sismaker.com/espacolaser/download_infinity_pages/44767"
SISMAKER_LOGIN = os.environ.get("SISMAKER_LOGIN", "franqueado.aryel.losi@espacolaser.com.br")
SISMAKER_SENHA = os.environ.get("SISMAKER_SENHA", "Espaco@Losiana1")
TOKENS_FILE    = os.environ.get("TOKENS_FILE", "tokens.json")
IMGBB_API_KEY  = os.environ.get("IMGBB_API_KEY", "")
TZ_SP          = pytz.timezone("America/Sao_Paulo")
HEADLESS       = os.environ.get("HEADLESS", "true").lower() == "true"
DOWNLOAD_DIR   = Path(tempfile.gettempdir()) / "sismaker_posts"

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
    hora  = datetime.now(TZ_SP).strftime("%H:%M")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"data": today, "hora": hora, "postado": True, "resumo": resumo}, f,
                  indent=2, ensure_ascii=False)


# ─── Notificação WhatsApp (Z-API) ─────────────────────────────────────────────

def enviar_whatsapp(mensagem: str):
    """Envia mensagem de texto via Z-API."""
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
            print("  WhatsApp enviado com sucesso!")
        else:
            print(f"  [AVISO] WhatsApp status {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  [AVISO] Falha ao enviar WhatsApp: {e}")


# ─── Gerenciamento de Tokens ──────────────────────────────────────────────────

def carregar_tokens() -> dict:
    if not Path(TOKENS_FILE).exists():
        raise FileNotFoundError(
            f"Arquivo '{TOKENS_FILE}' não encontrado.\n"
            "Execute primeiro: python token_helper.py"
        )
    with open(TOKENS_FILE, encoding="utf-8") as f:
        return json.load(f)


def salvar_tokens(data: dict):
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def verificar_e_renovar_token(loja: str, config: dict) -> str | None:
    """Verifica validade do token e tenta renovar se necessário."""
    token = config["accounts"][loja].get("token", "")
    if not token:
        print(f"  [{loja}] Token não configurado. Execute token_helper.py.")
        return None

    r = requests.get(
        "https://graph.facebook.com/v19.0/me",
        params={"access_token": token},
        timeout=10,
    )
    if r.status_code == 200:
        return token

    print(f"  [{loja}] Token inválido (código {r.status_code}), tentando renovar...")
    r2 = requests.get(
        "https://graph.facebook.com/v19.0/oauth/access_token",
        params={
            "grant_type":       "fb_exchange_token",
            "client_id":        config["app_id"],
            "client_secret":    config["app_secret"],
            "fb_exchange_token": token,
        },
        timeout=10,
    )
    data = r2.json()
    if "access_token" in data:
        novo = data["access_token"]
        config["accounts"][loja]["token"] = novo
        salvar_tokens(config)
        print(f"  [{loja}] Token renovado com sucesso.")
        return novo

    print(f"  [{loja}] Não foi possível renovar: {data.get('error', data)}")
    print(f"  [{loja}] Gere um novo token com: python token_helper.py")
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
            print(f"  Imagem {prefixo} recortada para 4:5 ({novo_w}x{h})")
        elif ratio > 1.92:
            # Muito largo — corta verticalmente para 1.91:1
            novo_h = int(w / 1.91)
            offset = (h - novo_h) // 2
            img = img.crop((0, offset, w, offset + novo_h))
            print(f"  Imagem {prefixo} recortada para 1.91:1 ({w}x{novo_h})")

    dest = src.with_suffix(".jpg")
    img.save(dest, "JPEG", quality=95)
    return dest


# ─── Upload de Imagem (imgbb) ─────────────────────────────────────────────────

def upload_imgbb(image_path: Path, api_key: str) -> str | None:
    """
    Faz upload da imagem no imgbb e retorna URL pública.
    A imagem expira em 10 minutos (suficiente para o Instagram processá-la).
    """
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    r = requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": api_key, "image": img_b64, "expiration": 600},
        timeout=30,
    )
    data = r.json()
    if data.get("success"):
        return data["data"]["url"]
    print(f"  [ERRO imgbb] {data}")
    return None


# ─── Instagram Graph API ──────────────────────────────────────────────────────

def postar_instagram(ig_user_id: str, token: str, image_url: str, caption: str,
                     is_story: bool = False, tentativas: int = 3) -> bool:
    """
    Publica feed ou story no Instagram via Graph API.
    Faz até `tentativas` retentativas em erros transitórios.
    """
    base = "https://graph.facebook.com/v19.0"
    tipo = "Story" if is_story else "Feed"

    # image_url deve ir nos params (query string), não no body — exigido pela API do Instagram
    api_params = {"access_token": token, "image_url": image_url}
    if is_story:
        api_params["media_type"] = "STORIES"

    # caption vai no body (pode ser longa, com emojis e hashtags)
    data_body = {} if is_story else {"caption": caption}

    for tentativa in range(1, tentativas + 1):
        if tentativa > 1:
            espera = tentativa * 10
            print(f"    Tentativa {tentativa}/{tentativas} em {espera}s...")
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
            print(f"    [ERRO] Criar container {tipo}: {err}")
            if is_transient and tentativa < tentativas:
                continue
            return False

        creation_id = d1["id"]
        print(f"    Container {tipo} criado: {creation_id}. Aguardando...")
        time.sleep(5)

        # Passo 2 — publicar
        r2 = requests.post(
            f"{base}/{ig_user_id}/media_publish",
            params={"access_token": token},
            data={"creation_id": creation_id},
            timeout=30,
        )
        d2 = r2.json()
        if "id" in d2:
            print(f"    {tipo} publicado! Post ID: {d2['id']}")
            return True

        err2 = d2.get("error", d2)
        print(f"    [ERRO] Publicar {tipo}: {err2}")
        return False

    return False


# ─── Helpers de arquivo ──────────────────────────────────────────────────────

def extrair_imagem_do_zip(zip_path: Path, prefixo: str, data_iso: str) -> tuple:
    """Extrai imagem e tenta extrair legenda (TXT/PDF) de um ZIP.
    Retorna: (Path|None, str|None) — (caminho_imagem, texto_legenda)
    """
    extensoes_imagem = {".jpg", ".jpeg", ".png"}
    img_path = None
    caption_text = None
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            nomes = z.namelist()
            imagens = [n for n in nomes if Path(n).suffix.lower() in extensoes_imagem]
            textos  = [n for n in nomes if Path(n).suffix.lower() == ".txt"]
            pdfs    = [n for n in nomes if Path(n).suffix.lower() == ".pdf"]

            # Extrai imagem (maior arquivo = maior qualidade)
            if imagens:
                imagens.sort(key=lambda n: z.getinfo(n).file_size, reverse=True)
                nome_img = imagens[0]
                ext = Path(nome_img).suffix
                dest = DOWNLOAD_DIR / f"{prefixo}_{data_iso}{ext}"
                dest.write_bytes(z.read(nome_img))
                img_path = dest
                print(f"  {prefixo.capitalize()} extraído do ZIP: {dest.name}")
            else:
                print(f"  [AVISO] ZIP sem imagens: {zip_path.name}")

            # Tenta extrair legenda de TXT
            if textos:
                try:
                    txt = z.read(textos[0]).decode("utf-8", errors="ignore").strip()
                    if len(txt) > 5:
                        caption_text = txt
                        print(f"  Legenda TXT encontrada no ZIP ({len(txt)} chars)")
                except Exception as e:
                    print(f"  [AVISO] Erro ao ler TXT do ZIP: {e}")

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
                        print(f"  Legenda PDF encontrada no ZIP ({len(texto_pdf)} chars)")
                except ImportError:
                    print(f"  [AVISO] pdfplumber não instalado — legenda PDF ignorada")
                except Exception as e:
                    print(f"  [AVISO] Erro ao ler PDF do ZIP: {e}")

    except Exception as e:
        print(f"  [AVISO] Erro ao extrair ZIP {zip_path.name}: {e}")
    return img_path, caption_text


# ─── Scraping do Sismaker ─────────────────────────────────────────────────────

def buscar_posts(data_alvo: str = "") -> dict:
    """
    Faz login no Sismaker, clica no card da data, baixa Feed e Story.

    data_alvo: "dd/mm" para forçar uma data específica (ex: "02/04").
               Se vazio, usa a data de hoje.

    Retorna:
        {
          "feed":    Path ou None,
          "story":   Path ou None,
          "caption": str
        }
    """
    hoje_dt = datetime.now(TZ_SP)

    if data_alvo:
        # Permite "02/04" ou "02/04/2026"
        partes = data_alvo.split("/")
        dia, mes = partes[0], partes[1]
        ano = partes[2] if len(partes) == 3 else str(hoje_dt.year)
        data_curta = f"{dia}/{mes}"            # "02/04"
        data_longa = f"{dia}/{mes}/{ano}"      # "02/04/2026"
        data_iso   = f"{ano}-{mes}-{dia}"      # "2026-04-02"
    else:
        data_curta = hoje_dt.strftime("%d/%m")       # "03/04"
        data_longa = hoje_dt.strftime("%d/%m/%Y")    # "03/04/2026"
        data_iso   = hoje_dt.strftime("%Y-%m-%d")    # "2026-04-03"

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # feeds e stories são listas de {"path": Path, "caption": str}
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
            print(f"  Abrindo Sismaker...")
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
            print(f"  Login realizado.")

            # ── 2. Página de cards com datas ──────────────────────────────
            page.goto(SISMAKER_URL, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            page.screenshot(path="debug_sismaker_cards.png")

            # Procura o card da data (aceita "02/04" ou "02/04/2026")
            card = None
            for texto in [data_longa, data_curta]:
                loc = page.get_by_text(texto, exact=True)
                if loc.count() > 0:
                    card = loc.first
                    print(f"  Card encontrado: '{texto}'")
                    break

            if card is None:
                print(f"  Nenhum card para {data_longa} ({data_curta}) encontrado.")
                print(f"  Verifique debug_sismaker_cards.png.")
                return resultado

            # ── 3. Entrar no card ─────────────────────────────────────────
            card.click()
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(1500)
            page.screenshot(path="debug_sismaker_conteudo.png")
            print(f"  Página de conteúdo aberta.")

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
                        print(f"  Legenda encontrada: {txt[:60]}...")
                        break
                except:
                    pass

            # ── 5. Baixar todos os arquivos disponíveis ────────────────────
            secoes = page.locator('text="Baixar"')
            qtd = secoes.count()
            print(f"  {qtd} botão(ões) 'Baixar' encontrado(s).")

            def baixar_arquivo(idx: int, label: str) -> tuple:
                """Clica em Baixar, salva e extrai ZIP se necessário.
                Retorna: (Path|None, str|None)
                """
                try:
                    with page.expect_download(timeout=25000) as dl_info:
                        secoes.nth(idx).click()
                    dl = dl_info.value
                    dest_raw = DOWNLOAD_DIR / f"raw_{label}_{data_iso}{Path(dl.suggested_filename).suffix}"
                    dl.save_as(str(dest_raw))
                    if dest_raw.suffix.lower() == ".zip":
                        return extrair_imagem_do_zip(dest_raw, label, data_iso)
                    return dest_raw, None
                except Exception as e:
                    print(f"  [AVISO] Download {label}: {e}")
                    return None, None

            arquivos_baixados = []  # lista de (Path, caption_str)
            for i in range(qtd):  # baixa TODOS os botões disponíveis
                arq, cap = baixar_arquivo(i, str(i))
                if arq:
                    arquivos_baixados.append((arq, cap or CAPTION_PADRAO))
                page.wait_for_timeout(800)

            # ── 6. Classificar feed vs story pelo aspect ratio ─────────────
            # Story: 9:16 → ratio ≈ 0.56  |  Feed: 4:5 → ratio = 0.80+
            from PIL import Image as _PIL
            for arq, cap in arquivos_baixados:
                try:
                    img = _PIL.open(arq)
                    ratio = img.width / img.height
                    img.close()
                    if ratio < 0.7:
                        resultado["stories"].append({"path": arq, "caption": cap})
                        print(f"  → Story: {arq.name} ({ratio:.2f}) | legenda: {cap[:40]}...")
                    else:
                        resultado["feeds"].append({"path": arq, "caption": cap})
                        print(f"  → Feed:  {arq.name} ({ratio:.2f}) | legenda: {cap[:40]}...")
                except Exception as e:
                    print(f"  [AVISO] Não foi possível classificar {arq.name}: {e}")

        except Exception as e:
            print(f"  [ERRO] Sismaker: {e}")
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
    print(f"  Instagram Poster — {agora.strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    # 0. Verificar se já postou hoje (evita duplo disparo 10h→18h)
    if not DATA_ALVO and ja_postou_hoje():
        print("\n  Posts de hoje já foram publicados anteriormente. Encerrando.")
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

    feeds   = posts["feeds"]    # lista de {"path": Path, "caption": str}
    stories = posts["stories"]  # lista de {"path": Path, "caption": str}

    if not feeds and not stories:
        print(f"\n  Nenhum arquivo baixado para {label}. Encerrando.")
        return

    print(f"\n  Feeds encontrados:   {len(feeds)}")
    print(f"  Stories encontrados: {len(stories)}")

    # 3. Preparar imagens e fazer upload para URL pública
    # feed_urls e story_urls são listas de (url, caption)
    print("\n[2/3] Preparando e fazendo upload das imagens...")
    feed_urls  = []
    story_urls = []

    for i, item in enumerate(feeds):
        url = upload_imgbb(preparar_imagem(item["path"], f"feed_{i}"), imgbb_key)
        if url:
            feed_urls.append((url, item["caption"]))
            print(f"  Feed {i+1} URL ok | legenda: {item['caption'][:50]}...")

    for i, item in enumerate(stories):
        url = upload_imgbb(preparar_imagem(item["path"], f"story_{i}"), imgbb_key)
        if url:
            story_urls.append((url, item["caption"]))
            print(f"  Story {i+1} URL ok | legenda: {item['caption'][:50]}...")

    if not feed_urls and not story_urls:
        print("  Falha no upload de todas as imagens. Encerrando.")
        return

    # 4. Postar em cada conta
    print("\n[3/3] Postando no Instagram...")
    resultados = {}

    for loja, conta in config["accounts"].items():
        ig_user_id = conta.get("ig_user_id", "")
        if not ig_user_id:
            print(f"\n  [{loja}] Sem ig_user_id — pulando.")
            resultados[loja] = "IGNORADO (sem ig_user_id)"
            continue

        token = verificar_e_renovar_token(loja, config)
        if not token:
            resultados[loja] = "FALHA (token inválido)"
            continue

        print(f"\n  [{loja}]")
        ok_feeds   = []
        ok_stories = []

        for feed_url, cap in feed_urls:
            ok = postar_instagram(ig_user_id, token, feed_url, cap, is_story=False)
            ok_feeds.append(ok)
            time.sleep(3)

        for story_url, cap in story_urls:
            ok = postar_instagram(ig_user_id, token, story_url, cap, is_story=True)
            ok_stories.append(ok)
            time.sleep(3)

        partes = []
        if feed_urls:
            partes.append(f"Feeds={sum(ok_feeds)}/{len(ok_feeds)} OK")
        if story_urls:
            partes.append(f"Stories={sum(ok_stories)}/{len(ok_stories)} OK")
        resultados[loja] = " | ".join(partes)

    # Resumo final
    print("\n" + "=" * 60)
    print("  RESUMO:")
    for loja, status in resultados.items():
        print(f"  {loja:35s} → {status}")
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
            linhas.append(f"  {icone} @{loja}: {status}")
        linhas.append("\n_Enviado automaticamente pelo bot Espaço Laser_")

        print("\n  Enviando notificação WhatsApp...")
        enviar_whatsapp("\n".join(linhas))


if __name__ == "__main__":
    main()
