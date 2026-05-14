import requests
import json

def send_webhook_alert(webhook_url, title, message, status="danger"):
    """
    Dispara alertas para Microsoft Teams, Slack ou Discord.
    """
    if not webhook_url or webhook_url.strip() == "":
        return False, "Nenhuma URL de Webhook configurada."

    # Cores (Verde para sucesso, Vermelho para queda)
    color_hex = "#10b981" if status == "success" else "#ef4444"
    color_int = 11010049 if status == "success" else 15680512 # Usado pelo Discord

    url = webhook_url.lower()
    payload = {}

    try:
        # 1. Formato DISCORD
        if "discord.com" in url:
            payload = {
                "embeds": [{
                    "title": title,
                    "description": message,
                    "color": color_int
                }]
            }
            
        # 2. Formato SLACK
        elif "hooks.slack.com" in url:
            payload = {
                "text": f"*{title}*\n{message}"
            }
            
        # 3. Formato MICROSOFT TEAMS (Padrão MessageCard)
        else:
            payload = {
                "@type": "MessageCard",
                "@context": "http://schema.org/extensions",
                "themeColor": color_hex.replace("#", ""),
                "summary": title,
                "sections": [{
                    "activityTitle": f"**{title}**",
                    "activitySubtitle": message,
                    "markdown": True
                }]
            }

        headers = {'Content-Type': 'application/json'}
        response = requests.post(webhook_url, data=json.dumps(payload), headers=headers, timeout=5)
        
        if response.status_code in (200, 201, 202, 204):
            return True, "Webhook enviado com sucesso!"
        else:
            return False, f"Erro do servidor (Código {response.status_code}): {response.text}"
            
    except Exception as e:
        return False, f"Falha na comunicação: {str(e)}"