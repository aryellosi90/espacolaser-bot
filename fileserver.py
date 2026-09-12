"""
Servidor HTTP minimo que serve os arquivos de PUBLIC_UPLOAD_DIR.

Existe porque depender de hosts anonimos de terceiros (catbox.moe, imgbb)
pra disponibilizar a midia numa URL publica — que o Meta busca pra
processar Feed/Reels/Story — se mostrou pouco confiavel: throttling,
bloqueio ao fetcher do proprio Meta, mudanca de politica sem aviso (tudo
observado entre 09 e 12/09/2026, mesmo com uploads bem-sucedidos do nosso
lado). Servir do nosso proprio domínio Railway tira essa dependencia.

Roda em background junto com o cron (ver CMD do Dockerfile.instagram).
"""
import http.server
import os

PORT = int(os.environ.get("FILESERVER_PORT", "8080"))
DIRETORIO = os.environ.get("PUBLIC_UPLOAD_DIR", "/app/public_uploads")

os.makedirs(DIRETORIO, exist_ok=True)
os.chdir(DIRETORIO)

class _SemListagemDeDiretorio(http.server.SimpleHTTPRequestHandler):
    """Serve arquivos normalmente, mas nunca lista o conteúdo do diretório —
    os nomes são aleatórios (uuid4), mas não custa nada evitar expor a lista
    inteira de mídias já publicadas pra quem acessar a URL base."""

    def list_directory(self, path):
        self.send_error(403, "Listagem de diretório desabilitada")
        return None

    def log_message(self, format, *args):
        print(f"[fileserver] {self.address_string()} - {format % args}", flush=True)

if __name__ == "__main__":
    handler = _SemListagemDeDiretorio
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler) as httpd:
        print(f"[fileserver] servindo {DIRETORIO} em 0.0.0.0:{PORT}", flush=True)
        httpd.serve_forever()
