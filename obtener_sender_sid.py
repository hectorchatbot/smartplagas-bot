from twilio.rest import Client
import os
from dotenv import load_dotenv

# Cargar variables del .env
load_dotenv()

# Datos de acceso
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
client = Client(account_sid, auth_token)

# Reemplaza con tu SID del Messaging Service
service_sid = "MG189257910f5d0d96e3715dece0e230ac"  # <-- COPIA AQUÍ EL TUYO

# Listar los números asociados al servicio
senders = client.messaging.services(service_sid).phone_numbers.list()

if not senders:
    print("❌ No hay números asociados a este servicio.")
else:
    for sender in senders:
        print("✅ Sender SID:", sender.sid)
        print("📱 Número:", sender.phone_number)
