"""
UmiClean — tienda con pago Wompi.

Estructura:
  app.py                  → servidor (precios, pagos, correos)
  public/index.html       → página principal
  public/gracias.html     → página después del pago
  public/politica.html    → plantilla de las políticas
  public/politicas/*.html → texto de cada política
  public/img/             → fotos de los productos

Variables de entorno (Railway → web → Variables):
  WOMPI_INTEGRITY_SECRET  → prod_integrity_...  (obligatoria)
  WOMPI_EVENTS_SECRET     → prod_events_...     (para el webhook)
  SMTP_PASSWORD           → contraseña de noreply@umiclean.co
"""
import os, json, time, random, string, hashlib, threading, smtplib, urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, render_template_string, redirect, request, jsonify, send_file, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE_DIR, "public")

app = Flask(__name__, static_folder=os.path.join(PUBLIC, "img"), static_url_path="/img")

# =========================
# DATOS DEL NEGOCIO
# =========================
WHATSAPP_NUMBER = "573106966593"
CITY = "Bogotá, Colombia"
SHIPPING_COST = 20000        # envío cuando el pedido no llega al mínimo
FREE_SHIPPING_MIN = 75000    # desde este subtotal el envío es gratis

# =========================
# PRECIOS — cambia aquí y se actualizan la página y el cobro
# =========================
PRODUCTOS = {
    "oxigeno": {"nombre": "Oxígeno Activo Fórmula Ultra", "precio": 25000},
    "kit":     {"nombre": "Shoe Cleaner Kit",              "precio": 25000},
    "gel":     {"nombre": "Gel Oxigenado 2L",              "precio": 15000},
}
# Los combos siempre tienen envío incluido
COMBOS = {
    "p1": {"nombre": "Combo Total (Oxígeno + Kit + Detergente)", "precio": 70000},
    "p2": {"nombre": "Combo Medias (Oxígeno + Gel)",             "precio": 35000},
    "p3": {"nombre": "Combo Zapatos (Kit + Gel)",                "precio": 35000},
    "p4": {"nombre": "Combo Completo (Los 3 productos)",         "precio": 50000},
}

# =========================
# WOMPI
# =========================
WOMPI_PUBLIC_KEY = os.environ.get("WOMPI_PUBLIC_KEY", "pub_prod_L0He5xTVgLMd2bWAY6r5u8PTl4m4BU0K")
WOMPI_INTEGRITY_SECRET = os.environ.get("WOMPI_INTEGRITY_SECRET", "")
WOMPI_EVENTS_SECRET = os.environ.get("WOMPI_EVENTS_SECRET", "")
WOMPI_API = ("https://sandbox.wompi.co/v1" if WOMPI_PUBLIC_KEY.startswith("pub_test_")
             else "https://production.wompi.co/v1")

# =========================
# CORREO (Titan)
# =========================
SMTP_SERVER = "smtp.titan.email"
SMTP_PORT = 465
SMTP_USER = "noreply@umiclean.co"
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")


# =========================
# UTILIDADES
# =========================
def cop(n):
    """25000 -> '25.000'"""
    return f"{int(n):,}".replace(",", ".")


def leer(ruta):
    with open(os.path.join(PUBLIC, ruta), encoding="utf-8") as f:
        return f.read()


def pagina(ruta, **datos):
    return render_template_string(leer(ruta), **datos)


# =========================
# PÁGINAS
# =========================
@app.route("/")
@app.route("/productos")
@app.route("/promociones")
def index():
    productos = {k: {**v, "precio_fmt": cop(v["precio"])} for k, v in PRODUCTOS.items()}
    combos = {k: {**v, "precio_fmt": cop(v["precio"])} for k, v in COMBOS.items()}
    return pagina(
        "index.html",
        whatsapp_number=WHATSAPP_NUMBER,
        city=CITY,
        shipping_cost=SHIPPING_COST,
        shipping_cost_fmt=cop(SHIPPING_COST),
        envio_gratis=FREE_SHIPPING_MIN,
        envio_gratis_fmt=cop(FREE_SHIPPING_MIN),
        productos=productos,
        combos=combos,
        productos_json=json.dumps(PRODUCTOS, ensure_ascii=False),
        combos_json=json.dumps(COMBOS, ensure_ascii=False),
    )


POLITICAS = {
    "envio": "Política de envío",
    "terminos": "Términos del servicio",
    "privacidad": "Política de privacidad",
    "reembolso": "Política de reembolso",
    "contacto": "Información de contacto",
}


@app.route("/politicas/<slug>")
def politica(slug):
    if slug not in POLITICAS:
        abort(404)
    return pagina("politica.html", titulo=POLITICAS[slug], contenido=leer(f"politicas/{slug}.html"))


# Links viejos
@app.route("/policies/<viejo>")
def politica_vieja(viejo):
    nuevo = {"shipping": "envio", "terms": "terminos", "privacy": "privacidad",
             "refund": "reembolso", "contact": "contacto"}.get(viejo)
    return redirect(f"/politicas/{nuevo}" if nuevo else "/", code=301)


@app.route("/pago")
def pago():
    return redirect(f"https://wa.me/{WHATSAPP_NUMBER}?text=Hola%20UmiClean%2C%20quiero%20hacer%20un%20pedido.%20%F0%9F%9B%92")


@app.route("/portafolio")
def portafolio():
    pdf = os.path.join(BASE_DIR, "UmiClean_Portafolio.pdf")
    if os.path.exists(pdf):
        return send_file(pdf, as_attachment=True, download_name="UmiClean_Portafolio.pdf")
    return redirect("/")


@app.route("/health")
def health():
    return jsonify(ok=True)


# =========================
# PEDIDOS (archivo JSON simple)
# =========================
PEDIDOS_FILE = os.environ.get("PEDIDOS_FILE", os.path.join(BASE_DIR, "pedidos.json"))
_lock = threading.Lock()


def cargar_pedidos():
    try:
        with open(PEDIDOS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_pedidos(pedidos):
    tmp = PEDIDOS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pedidos, f, ensure_ascii=False)
    os.replace(tmp, PEDIDOS_FILE)


def calcular_pedido(items_in):
    """Recalcula el pedido con los precios del servidor (el navegador no decide cuánto se cobra)."""
    items, subtotal, tiene_combo = [], 0, False
    for it in items_in or []:
        tipo, key = str(it.get("type")), str(it.get("key"))
        catalogo = PRODUCTOS if tipo == "product" else COMBOS if tipo == "promo" else {}
        if key not in catalogo:
            raise ValueError("Producto inválido")
        qty = int(it.get("qty", 1))
        if not 1 <= qty <= 50:
            raise ValueError("Cantidad inválida")
        p = catalogo[key]
        items.append({"type": tipo, "key": key, "name": p["nombre"], "price": p["precio"],
                      "qty": qty, "total": p["precio"] * qty})
        subtotal += p["precio"] * qty
        tiene_combo = tiene_combo or tipo == "promo"
    if not items:
        raise ValueError("Carrito vacío")
    envio = 0 if (tiene_combo or subtotal >= FREE_SHIPPING_MIN) else SHIPPING_COST
    return items, subtotal, envio, subtotal + envio


# =========================
# WOMPI
# =========================
def firma_integridad(referencia, centavos, moneda="COP"):
    return hashlib.sha256(f"{referencia}{centavos}{moneda}{WOMPI_INTEGRITY_SECRET}".encode()).hexdigest()


def consultar_transaccion(tx_id):
    url = f"{WOMPI_API}/transactions/{urllib.request.quote(str(tx_id))}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {WOMPI_PUBLIC_KEY}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode()).get("data", {})


def registrar_transaccion(tx):
    """Actualiza el pedido con el estado de Wompi. Si quedó APPROVED manda los correos (una sola vez)."""
    ref, status = tx.get("reference"), tx.get("status")
    enviar = None
    with _lock:
        pedidos = cargar_pedidos()
        p = pedidos.get(ref)
        if not p:
            return None
        if status == "APPROVED" and int(tx.get("amount_in_cents", -1)) != p["amount_in_cents"]:
            status = "MONTO_INVALIDO"
        p["status"], p["transaction_id"] = status, tx.get("id")
        if status == "APPROVED" and not p.get("correos_enviados"):
            p["correos_enviados"] = True
            enviar = dict(p)
        guardar_pedidos(pedidos)
    if enviar:
        enviar_correos(enviar)
    return p


@app.route("/wompi/iniciar", methods=["POST"])
def wompi_iniciar():
    if not WOMPI_INTEGRITY_SECRET:
        return jsonify(ok=False, error="Pagos no configurados (falta WOMPI_INTEGRITY_SECRET)"), 500
    data = request.get_json(silent=True) or {}
    cliente = {k: str((data.get("cliente") or {}).get(k, "")).strip()[:200]
               for k in ("nombre", "celular", "email", "direccion", "ciudad")}
    if not all(cliente.values()):
        return jsonify(ok=False, error="Faltan datos del cliente"), 400
    try:
        items, subtotal, envio, total = calcular_pedido(data.get("items"))
    except (ValueError, TypeError) as e:
        return jsonify(ok=False, error=str(e)), 400

    ref = f"UMI-{int(time.time() * 1000)}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"
    centavos = total * 100
    with _lock:
        pedidos = cargar_pedidos()
        pedidos[ref] = {"ref": ref, "cliente": cliente, "items": items, "subtotal": subtotal,
                        "envio": envio, "total": total, "amount_in_cents": centavos,
                        "status": "PENDING", "creado": datetime.now().isoformat(timespec="seconds")}
        guardar_pedidos(pedidos)

    return jsonify(ok=True, reference=ref, amountInCents=centavos,
                   publicKey=WOMPI_PUBLIC_KEY, signature=firma_integridad(ref, centavos))


@app.route("/wompi/eventos", methods=["POST"])
def wompi_eventos():
    """Webhook de Wompi. URL a configurar en Wompi: https://www.umiclean.co/wompi/eventos"""
    ev = request.get_json(silent=True) or {}
    try:
        sig = ev.get("signature", {})
        valores = ""
        for prop in sig.get("properties", []):
            v = ev.get("data", {})
            for parte in prop.split("."):
                v = v.get(parte, "")
            valores += str(v)
        esperado = hashlib.sha256(f"{valores}{ev.get('timestamp', '')}{WOMPI_EVENTS_SECRET}".encode()).hexdigest()
        if not WOMPI_EVENTS_SECRET or esperado.lower() != str(sig.get("checksum", "")).lower():
            return jsonify(ok=False), 401
        if ev.get("event") == "transaction.updated":
            registrar_transaccion(ev["data"]["transaction"])
    except Exception as e:
        print(f"Error webhook Wompi: {e}")
    return jsonify(ok=True)


@app.route("/gracias")
def gracias():
    tx_id = request.args.get("id", "")
    estado, ref = None, "—"
    if tx_id:
        try:
            tx = consultar_transaccion(tx_id)
            ref = tx.get("reference", "—")
            p = registrar_transaccion(tx)
            estado = p["status"] if p else tx.get("status")
        except Exception as e:
            print(f"Error consultando transacción {tx_id}: {e}")

    if estado == "APPROVED":
        icono, titulo = "✅", "¡Pago aprobado!"
        mensaje = ("💬 <strong>Próximo paso:</strong> Te enviamos la confirmación a tu correo "
                   "y te contactaremos por WhatsApp para coordinar la entrega.")
    elif estado == "PENDING":
        icono, titulo = "⏳", "Pago en proceso"
        mensaje = "Tu pago está siendo procesado. Cuando se apruebe te llegará la confirmación a tu correo."
    elif estado in ("DECLINED", "VOIDED", "ERROR", "MONTO_INVALIDO"):
        icono, titulo = "❌", "Pago no aprobado"
        mensaje = ("Tu pago no fue aprobado y no se realizó ningún cobro. "
                   "Puedes intentarlo de nuevo o escribirnos por WhatsApp.")
    else:
        icono, titulo = "ℹ️", "Estamos verificando tu pago"
        mensaje = ("No pudimos confirmar el estado del pago todavía. "
                   "Si se realizó el cobro, escríbenos por WhatsApp con tu referencia.")

    return pagina("gracias.html", order_id=ref, fecha=datetime.now().strftime("%d/%m/%Y · %I:%M %p"),
                  whatsapp_number=WHATSAPP_NUMBER, icono=icono, titulo=titulo, mensaje=mensaje)


# =========================
# CORREOS
# =========================
def enviar_correo(para, asunto, html):
    if not SMTP_PASSWORD:
        print("SMTP_PASSWORD no configurada: no se envió el correo")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = asunto, f"UmiClean <{SMTP_USER}>", para
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, para, msg.as_string())
    except Exception as e:
        print(f"Error enviando correo a {para}: {e}")


def tabla_items(items):
    filas = "".join(
        f"<tr><td style='padding:8px;border-bottom:1px solid #eee'>{it['name']}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:right'>x{it['qty']}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:right'>${cop(it['total'])} COP</td></tr>"
        for it in items)
    return ("<table style='width:100%;border-collapse:collapse;margin:16px 0'>"
            "<thead><tr style='background:#0d2b52;color:white'>"
            "<th style='padding:10px;text-align:left'>Producto</th>"
            "<th style='padding:10px;text-align:right'>Cant.</th>"
            "<th style='padding:10px;text-align:right'>Valor</th></tr></thead>"
            f"<tbody>{filas}</tbody></table>")


def enviar_correos(p):
    c, ref = p["cliente"], p["ref"]
    envio = "GRATIS" if p["envio"] == 0 else f"${cop(p['envio'])} COP"
    fecha = datetime.now().strftime("%d/%m/%Y %I:%M %p")

    # 1. Al cliente
    enviar_correo(c["email"], f"✅ ¡Gracias por tu compra en UmiClean! #{ref}", f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
      <div style="background:#0d2b52;padding:32px;text-align:center;border-radius:12px 12px 0 0">
        <h1 style="color:white;margin:0">UmiClean</h1>
        <p style="color:#00c9a7;margin:8px 0 0">Oxígeno Activo · Sin Cloro</p>
      </div>
      <div style="background:#f7f9fc;padding:32px;border-radius:0 0 12px 12px">
        <h2 style="color:#0d2b52">¡Gracias por tu compra, {c['nombre']}! 🎉</h2>
        <p style="color:#555">Tu pago fue aprobado. Te contactaremos por WhatsApp para coordinar la entrega.</p>
        <p style="color:#888;font-size:.85rem">Referencia <b style="color:#0d2b52">#{ref}</b> · {fecha}</p>
        <p style="color:#2e7d32"><b>📍 Envío a:</b> {c['direccion']}, {c['ciudad']} · 📱 {c['celular']}</p>
        {tabla_items(p['items'])}
        <p style="color:#0d2b52"><b>Envío:</b> {envio}<br><b>TOTAL PAGADO: ${cop(p['total'])} COP</b></p>
        <p style="text-align:center;color:#888;font-size:.8rem;margin-top:24px">UmiClean · {CITY} · umiclean.co</p>
      </div>
    </div>""")

    # 2. A UmiClean
    wa = (f"https://wa.me/57{c['celular']}?text=" +
          urllib.request.quote(f"Hola {c['nombre']}, tu pedido UmiClean #{ref} fue confirmado y está en camino 🚚 ¡Gracias por tu compra!"))
    enviar_correo(SMTP_USER, f"🛒 Nuevo pedido #{ref} — {c['nombre']} — ${cop(p['total'])} COP", f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:32px;border:2px solid #0d2b52;border-radius:12px">
      <h2 style="color:#0d2b52">🛒 Nuevo pedido PAGADO</h2>
      <p>Referencia: <b>{ref}</b> · {fecha} · Wompi: {p.get('transaction_id', '')}</p>
      <p>👤 {c['nombre']}<br>📱 {c['celular']}<br>✉️ {c['email']}<br>📍 {c['direccion']}, {c['ciudad']}</p>
      {tabla_items(p['items'])}
      <p style="font-size:1.1rem;color:#0d2b52"><b>TOTAL: ${cop(p['total'])} COP</b> · Envío: {envio}</p>
      <a href="{wa}" style="display:inline-block;background:#25d366;color:white;padding:14px 24px;border-radius:999px;font-weight:700;text-decoration:none">💬 Escribirle por WhatsApp</a>
    </div>""")


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
