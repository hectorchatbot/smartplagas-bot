from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import json
import logging
import os
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

sesiones = {}

def obtener_bloque_por_id(bloque_id):
    for bloque in flujo:
        if str(bloque["id"]) == str(bloque_id):
            return bloque
    return None

def reemplazar_variables(texto, datos):
    for clave, valor in datos.items():
        texto = texto.replace(f"{{{clave}}}", valor)
    return texto

def avanzar_mensajes_automaticos(sender, bloque_actual, respuesta_twilio):
    while bloque_actual and bloque_actual["type"] == "mensaje":
        contenido = reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"])
        logging.info(f"[AUTO] Enviando mensaje: {contenido}")
        respuesta_twilio.message(contenido)
        siguiente_id = bloque_actual.get("nextId")
        if not siguiente_id:
            return None
        bloque_actual = obtener_bloque_por_id(siguiente_id)
        sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None
    return bloque_actual

def enviar_resumen_por_whatsapp(data_cliente):
    try:
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        from_whatsapp = os.getenv("TWILIO_WHATSAPP_FROM")
        to_whatsapp = os.getenv("NOTIFICACION_TELEFONO")

        client = Client(account_sid, auth_token)

        mensaje = f"""🟢 NUEVO CLIENTE SMART PLAGAS

📛 Nombre: {data_cliente.get('nombre', '')}
🏠 Dirección: {data_cliente.get('direccion', '')}
🏙️ Comuna: {data_cliente.get('comuna', '')}
📞 Teléfono: {data_cliente.get('telefono', '')}
✉️ Email: {data_cliente.get('email', '')}
🏷️ Tipo de cliente: {data_cliente.get('tipo_cliente', 'No indicado')}
🛠️ Servicio solicitado: {data_cliente.get('servicio', 'No indicado')}
🔧 Subservicio: {data_cliente.get('subservicio', 'No indicado')}
📍 Área requerida: {data_cliente.get('subarea', '')}
📐 Metros cuadrados: {data_cliente.get('cantidad_metros cuadrados', '')}
🏊 Tamaño piscina: {data_cliente.get('tamano_piscina', '')}
🔩 Material piscina: {data_cliente.get('tipo_material', '')}
🎥 Cantidad cámaras: {data_cliente.get('cantidad_camara', '')}
📷 Tipo de cámara: {data_cliente.get('tipo_camara', '')}
📡 Área a vigilar: {data_cliente.get('¿Qué áreas deseas vigilar?', '')}
📲 Acceso remoto: {data_cliente.get('¿Podrías tener acceso remoto desde celular o PC una vez instalado el sistema?', '')}
🌐 Conexión a internet: {data_cliente.get('¿Cuenta con conexión a internet en el lugar de instalación?', '')}
📝 Observaciones: {data_cliente.get('detalles', 'No hay detalles adicionales')}
"""

        client.messages.create(
            body=mensaje,
            from_=from_whatsapp,
            to=to_whatsapp
        )

    except Exception as e:
        logging.error(f"❌ Error al enviar resumen por WhatsApp: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        sender = request.form.get('From')
        msg = request.form.get('Body', '').strip()
        logging.info(f"Mensaje de {sender}: {msg}")

        respuesta = MessagingResponse()

        if sender not in sesiones or msg.lower() == "hola":
            sesiones[sender] = {
                "current_id": str(flujo[0]["id"]),
                "data": {}
            }
            logging.info("Nueva sesión iniciada")

        bloque_actual = obtener_bloque_por_id(sesiones[sender]["current_id"])
        if not bloque_actual:
            raise Exception("Bloque actual no encontrado")

        tipo = bloque_actual["type"]

        if tipo == "pregunta":
            var = bloque_actual.get("variableName")
            if var:
                sesiones[sender]["data"][var] = msg
            siguiente_id = bloque_actual.get("nextId")
            bloque_actual = obtener_bloque_por_id(siguiente_id)
            sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

        elif tipo == "condicional":
            msg_normalizado = msg.lower()
            seleccion = None
            for i, opcion in enumerate(bloque_actual["options"], 1):
                texto_opcion = opcion["text"].lower()
                if msg_normalizado == str(i) or msg_normalizado in texto_opcion:
                    seleccion = opcion
                    break
            if seleccion:
                if "saveAs" in seleccion:
                    sesiones[sender]["data"][seleccion["saveAs"]] = seleccion["text"]
                siguiente_id = seleccion.get("nextId")
                bloque_actual = obtener_bloque_por_id(siguiente_id)
                sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None
            else:
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual["options"])])
                respuesta.message(f"Opción inválida. Elige una de estas:\n{opciones}")
                return str(respuesta)

        bloque_actual = avanzar_mensajes_automaticos(sender, bloque_actual, respuesta)

        if bloque_actual:
            tipo = bloque_actual["type"]
            if tipo == "pregunta":
                contenido = reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"])
                respuesta.message(contenido)
            elif tipo == "condicional":
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual["options"])])
                respuesta.message(f"{bloque_actual['content']}\n{opciones}")
        else:
            # El flujo terminó, enviar resumen por WhatsApp
            enviar_resumen_por_whatsapp(sesiones[sender]["data"])

        return str(respuesta)

    except Exception as e:
        logging.exception("❌ Error inesperado:")
        respuesta = MessagingResponse()
        respuesta.message("Ha ocurrido un error. Intenta nuevamente.")
        return str(respuesta)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
