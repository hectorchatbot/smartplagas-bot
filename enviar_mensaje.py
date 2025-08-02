import os
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
from_whatsapp_number = os.getenv("TWILIO_WHATSAPP_FROM")

client = Client(account_sid, auth_token)

def enviar_mensaje(numero_destino, texto):
    message = client.messages.create(
        body=texto,
        from_=from_whatsapp_number,
        to=f"whatsapp:{numero_destino}"
    )
    print(f"✅ Mensaje enviado con SID: {message.sid}")


