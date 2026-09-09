"""
Grava as variaveis de ambiente do processo atual (as que o Railway injeta)
num arquivo que o cron consegue "source" com segurança.

cron roda com um ambiente minimo, sem as variaveis do Railway/Docker — por
isso o CMD do container grava esse snapshot uma vez, e o crontab da
"source" nele antes de chamar o instagram_poster.py.

`printenv > arquivo` (o jeito antigo) NAO escapa os valores — quebra assim
que algum valor tem aspas, chaves, cifrao etc, como o TOKENS_JSON. Usar
shlex.quote() garante que cada valor vira um literal de shell valido,
não importa o que tenha dentro.
"""
import os
import shlex

with open("/etc/container_env", "w", encoding="utf-8") as f:
    for key, value in os.environ.items():
        f.write(f"export {key}={shlex.quote(value)}\n")
