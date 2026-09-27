FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium

# torre_run.py: avisa a Torre de Controle como foi cada execução (ver o arquivo)
COPY bot.py torre_run.py .

CMD ["python", "torre_run.py", "bot_venda", "bot.py"]
