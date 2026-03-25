"""
Agendador do Bot de Agendamentos - Espaço Laser
================================================
Horários de execução:
  Seg a Sex : 10h, 12h, 14h, 16h, 18h, 20h
  Sábado    : 10h, 12h, 14h, 16h
  Domingo   : não executa

Para iniciar: python scheduler_agendamentos.py
Para parar : Ctrl+C
"""

import schedule
import time
import subprocess
import sys
import os
from datetime import datetime
import pytz

TZ_SP = pytz.timezone("America/Sao_Paulo")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(SCRIPT_DIR, "bot_agendamentos.py")
PYTHON_EXE = sys.executable

# Timeout máximo para o bot (segundos)
BOT_TIMEOUT = 360


def executar_bot():
    agora = datetime.now(TZ_SP)
    print(f"\n{'='*55}")
    print(f"  [{agora.strftime('%d/%m/%Y %H:%M')}] Executando bot de agendamentos...")
    print(f"{'='*55}")

    try:
        result = subprocess.run(
            [PYTHON_EXE, BOT_SCRIPT],
            cwd=SCRIPT_DIR,
            timeout=BOT_TIMEOUT,
        )
        status = "OK" if result.returncode == 0 else f"ERRO (código {result.returncode})"
        print(f"\n  Execução finalizada: {status}")
    except subprocess.TimeoutExpired:
        print(f"\n  ERRO: Bot não terminou em {BOT_TIMEOUT}s (timeout).")
    except FileNotFoundError:
        print(f"\n  ERRO: Arquivo não encontrado: {BOT_SCRIPT}")
    except Exception as e:
        print(f"\n  ERRO inesperado: {e}")


def configurar_horarios():
    """Configura todos os horários de execução."""

    # Segunda a Sexta: 10h até 20h (a cada 2h)
    horarios_semana = [10, 12, 14, 16, 18, 20]
    dias_semana = [
        schedule.every().monday,
        schedule.every().tuesday,
        schedule.every().wednesday,
        schedule.every().thursday,
        schedule.every().friday,
    ]
    for dia in dias_semana:
        for hora in horarios_semana:
            dia.at(f"{hora:02d}:00").do(executar_bot)

    # Sábado: 10h até 16h (a cada 2h)
    horarios_sabado = [10, 12, 14, 16]
    for hora in horarios_sabado:
        schedule.every().saturday.at(f"{hora:02d}:00").do(executar_bot)


def proxima_execucao():
    """Retorna a próxima execução agendada."""
    jobs = schedule.get_jobs()
    if not jobs:
        return None
    prox = min(jobs, key=lambda j: j.next_run)
    return prox.next_run


def imprimir_cabecalho():
    print("╔══════════════════════════════════════════════════════╗")
    print("║        Bot Agendamentos — Espaço Laser               ║")
    print("╠══════════════════════════════════════════════════════╣")
    print("║  Seg a Sex : 10h | 12h | 14h | 16h | 18h | 20h      ║")
    print("║  Sábado    : 10h | 12h | 14h | 16h                   ║")
    print("║  Domingo   : sem execução                            ║")
    print("╠══════════════════════════════════════════════════════╣")

    prox = proxima_execucao()
    if prox:
        print(f"║  Próxima execução : {prox.strftime('%d/%m/%Y %H:%M')}                   ║")
    else:
        print("║  Nenhuma execução agendada                           ║")

    print("╠══════════════════════════════════════════════════════╣")
    print("║  Pressione Ctrl+C para encerrar                      ║")
    print("╚══════════════════════════════════════════════════════╝\n")


def main():
    configurar_horarios()
    imprimir_cabecalho()

    ultima_exibicao_prox = None

    while True:
        schedule.run_pending()

        # Mostra próxima execução a cada hora (ou quando muda)
        prox = proxima_execucao()
        if prox and prox != ultima_exibicao_prox:
            agora = datetime.now(TZ_SP)
            diff = prox - agora.replace(tzinfo=None)
            horas = int(diff.total_seconds() // 3600)
            mins = int((diff.total_seconds() % 3600) // 60)
            print(f"[{agora.strftime('%H:%M')}] Próxima execução: {prox.strftime('%d/%m/%Y %H:%M')} "
                  f"(em {horas}h {mins}min)")
            ultima_exibicao_prox = prox

        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAgendador encerrado pelo usuário.")
