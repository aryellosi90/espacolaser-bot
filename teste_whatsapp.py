import requests

ZAPI_INSTANCE = "3EB20541D8D291DF679CBE1FFBCB878E"
ZAPI_TOKEN    = "C3ACF20485B471C0AF93AE1A"
ZAPI_PHONE    = "5511964115060"

ZAPI_CLIENT_TOKEN = "F36133724cab54a22b90414da356600faS"

url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"
r = requests.post(
    url,
    json={"phone": ZAPI_PHONE, "message": "Teste WhatsApp bot Instagram OK!"},
    headers={"Client-Token": ZAPI_CLIENT_TOKEN},
    timeout=15,
)
print(r.status_code, r.text)
