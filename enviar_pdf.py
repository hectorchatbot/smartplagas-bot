from twilio.rest import Client
import os
from dotenv import load_dotenv
load_dotenv()

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
from_whatsapp_number = "whatsapp:+56958166055"

client = Client(account_sid, auth_token)

def enviar_cotizacion_pdf():
    numero_cliente = "whatsapp:+56955139922"
    url_pdf = "https://web-production-fa2ab.up.railway.app/static/cotizaciones/Cotizacion_Caren_Sector%20Puraquina_17_08_2025.pdf"

    message = client.messages.create(
        from_=from_whatsapp_number,
        to=numero_cliente,
        media_url=[url_pdf],
        body="Hola 👋, adjuntamos la cotización solicitada. Puedes revisar el PDF. ¡Gracias por confiar en Smart Plagas!"
    )

    print("✅ Cotización enviada por WhatsApp. SID:", message.sid)

if __name__ == "__main__":
    enviar_cotizacion_pdf()
