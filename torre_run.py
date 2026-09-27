"""
torre_run.py — roda um bot e avisa a Torre de Controle como foi.

    python torre_run.py <agente> <script.py> [args...]
    ex: python torre_run.py bot_venda bot.py

O bot roda exatamente como antes (mesmo Python, mesmos argumentos, mesmo
código de saída) e o log dele continua aparecendo no Railway. O porteiro só
lê o log para classificar a execução: os bots imprimem os erros e terminam
com código 0, então o código de saída sozinho não diz se deu certo.

Se TORRE_URL/TORRE_TOKEN não estiverem definidos, ou se a torre estiver fora
do ar, o bot roda normalmente e nada é avisado.

Variáveis: TORRE_URL (ex: https://torre.espacolaserlojas.com.br), TORRE_TOKEN
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import deque

TIMEOUT_AVISO = 8

# Como ler o log de cada agente: (padrão, status, resumo). A primeira regra
# de erro que casar vence; se nenhuma casar, vale a primeira regra de ok.
REGRAS = {
    "bot_venda": [
        (r"^Erro", "error", "falhou: {linha}"),
        (r"^Resposta: [45]\d\d", "error", "N8N recusou o envio ({linha})"),
        (r"^Resposta: 2\d\d", "ok", "enviou vendas do dia no grupo"),
        (r"^Dados já enviados", "ok", "sem venda nova — nada enviado"),
        (r"^Sem vendas no momento", "ok", "nenhuma venda ainda hoje"),
    ],
    "bot_agenda": [
        (r"ERRO CRÍTICO|Encerrado com erro no download", "error", "não conseguiu baixar do EVUP"),
        (r"ERRO webhook", "error", "falhou ao enviar para o N8N"),
        (r"Webhook \[\w+\]: [45]\d\d", "error", "N8N recusou o envio ({linha})"),
        (r"ERRO", "warn", "concluiu com aviso: {linha}"),
        (r"Webhook \[\w+\]: 2\d\d", "ok", "enviou {n_ok} relatório(s) no grupo"),
        (r"Concluído", "ok", "concluiu sem enviar nada"),
    ],
    # Mídias Digitais = instagram_poster.py (Sismaker → Instagram das lojas)
    "midias": [
        (r"^\[ERRO\]", "error", "falhou: {linha}"),
        (r"Falha no upload de todas", "error", "não conseguiu subir as imagens"),
        (r"publicado! Post ID", "ok", "publicou {n_pub} post(s) no Instagram"),
        (r"já foram publicados anteriormente", "ok", "posts de hoje já estavam publicados"),
        (r"Nenhum arquivo baixado", "ok", "nenhuma arte no Sismaker ainda"),
    ],
}


def avisar(payload):
    url, token = os.environ.get("TORRE_URL"), os.environ.get("TORRE_TOKEN")
    if not url or not token:
        return None
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/runs",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_AVISO) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # a torre nunca atrapalha o bot
        print(f"[torre] aviso não enviado: {e}", flush=True)
        return None


def classificar(agente, linhas, codigo):
    regras = REGRAS.get(agente, [])
    n_ok = sum(1 for l in linhas if re.search(r"Webhook \[\w+\]: 2\d\d", l))
    n_pub = sum(1 for l in linhas if "publicado! Post ID" in l)
    achados = []
    for padrao, status, resumo in regras:
        for l in linhas:
            if re.search(padrao, l.strip()):
                achados.append((status, resumo.format(linha=l.strip()[:120], n_ok=n_ok, n_pub=n_pub)))
                break
    for prioridade in ("error", "warn", "ok"):
        for status, resumo in achados:
            if status == prioridade:
                if codigo != 0 and status != "error":
                    return "error", f"terminou com código {codigo}"
                return status, resumo
    if codigo != 0:
        return "error", f"terminou com código {codigo}"
    return "warn", "terminou sem mensagem reconhecida"


def main():
    if len(sys.argv) < 3:
        print("uso: python torre_run.py <agente> <script.py> [args...]")
        sys.exit(2)
    agente, cmd = sys.argv[1], [sys.executable, "-u"] + sys.argv[2:]

    inicio = avisar({"agent": agente, "event": "start"})
    run_id = (inicio or {}).get("id")

    ultimas = deque(maxlen=400)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except Exception as e:
        proc = None
        ultimas.append(f"Erro ao iniciar: {e}")
        codigo = 1
    if proc:
        for bruto in proc.stdout:
            try:  # repassa o log do bot como veio; falha aqui nunca interrompe o bot
                sys.stdout.buffer.write(bruto)
                sys.stdout.buffer.flush()
            except Exception:
                pass
            ultimas.append(bruto.decode("utf-8", errors="replace").rstrip("\r\n"))
        codigo = proc.wait()

    status, resumo = classificar(agente, list(ultimas), codigo)
    detalhes = "\n".join(list(ultimas)[-60:])[-3000:]
    fim = {"status": status, "summary": resumo[:200], "details": detalhes}
    avisar({**fim, "id": run_id, "event": "finish"} if run_id else {**fim, "agent": agente})
    sys.exit(codigo)


if __name__ == "__main__":
    main()
