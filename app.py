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
import os, json, time, random, string, hashlib, hmac, threading, smtplib, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
# CORREO
# =========================
# Railway (plan Hobby) bloquea SMTP, así que se usa una API por HTTPS si está configurada:
#   RESEND_API_KEY  → resend.com  (recomendado)
#   BREVO_API_KEY   → brevo.com
# Si no hay ninguna, intenta SMTP con Titan (solo funciona en Railway Pro).
SMTP_SERVER = "smtp.titan.email"
SMTP_PORT = 465
SMTP_USER = "noreply@umiclean.co"
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
CORREO_ADMIN = os.environ.get("CORREO_ADMIN", SMTP_USER)  # adónde llegan los avisos y el reporte


# =========================
# UTILIDADES
# =========================
TZ = ZoneInfo("America/Bogota")


def ahora():
    """Hora de Colombia (el servidor de Railway está en UTC)."""
    return datetime.now(TZ)


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


# =========================
# BLOG — para agregar un artículo: crea public/blog/<slug>.html y añádelo aquí
# =========================
BLOG = {
    "percarbonato": {
        "kicker": "Guía completa",
        "titulo": "Percarbonato de sodio: qué es, para qué sirve y cómo usarlo",
        "resumen": "El ingrediente detrás de los blanqueadores sin cloro. Te explicamos cómo funciona y cómo sacarle el máximo provecho en la ropa y en la casa.",
    },
    "oxigeno-activo": {
        "kicker": "Limpieza sin cloro",
        "titulo": "Oxígeno activo: para qué sirve y por qué es mejor que el cloro",
        "resumen": "Blanquea, quita manchas y elimina olores sin dañar tu ropa. Así funciona y así lo usas con los productos UmiClean.",
    },
}


@app.route("/blog")
def blog():
    lista = "".join(
        f'<a class="post" href="/blog/{slug}"><div class="kicker">{a["kicker"]}</div>'
        f'<h2>{a["titulo"]}</h2><p>{a["resumen"]}</p><span class="leer">Leer artículo →</span></a>'
        for slug, a in BLOG.items())
    return pagina("blog.html", kicker="Blog UmiClean", titulo="Aprende a limpiar mejor, sin cloro",
                  resumen="Guías prácticas sobre oxígeno activo, percarbonato y trucos para tu ropa y tu casa.",
                  contenido=f'<div class="lista">{lista}</div>', es_articulo=False)


@app.route("/blog/<slug>")
def articulo(slug):
    if slug not in BLOG:
        abort(404)
    return pagina("blog.html", **BLOG[slug], contenido=leer(f"blog/{slug}.html"), es_articulo=True)


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
            p["pagado"] = ahora().isoformat(timespec="seconds")
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
                        "status": "PENDING", "creado": ahora().isoformat(timespec="seconds")}
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

    return pagina("gracias.html", order_id=ref, fecha=ahora().strftime("%d/%m/%Y · %I:%M %p"),
                  whatsapp_number=WHATSAPP_NUMBER, icono=icono, titulo=titulo, mensaje=mensaje)


# =========================
# CORREOS
# =========================
def _post_json(url, datos, headers):
    req = urllib.request.Request(url, data=json.dumps(datos).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def _enviar_ahora(para, asunto, html):
    remitente = f"UmiClean <{SMTP_USER}>"
    if RESEND_API_KEY:
        _post_json("https://api.resend.com/emails",
                   {"from": remitente, "to": [para], "subject": asunto, "html": html},
                   {"Authorization": f"Bearer {RESEND_API_KEY}"})
    elif BREVO_API_KEY:
        _post_json("https://api.brevo.com/v3/smtp/email",
                   {"sender": {"name": "UmiClean", "email": SMTP_USER}, "to": [{"email": para}],
                    "subject": asunto, "htmlContent": html},
                   {"api-key": BREVO_API_KEY})
    elif SMTP_PASSWORD:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = asunto, remitente, para
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, para, msg.as_string())
    else:
        print("Correo no configurado (falta RESEND_API_KEY): no se envió", asunto)
        return
    print(f"Correo enviado a {para}: {asunto}")


def enviar_correo(para, asunto, html):
    """Envía en segundo plano: nunca deja colgada la página del cliente."""
    def tarea():
        try:
            _enviar_ahora(para, asunto, html)
        except Exception as e:
            print(f"Error enviando correo a {para}: {e}")
    threading.Thread(target=tarea, daemon=True).start()


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
    fecha = ahora().strftime("%d/%m/%Y %I:%M %p")

    # 1. Al cliente
    enviar_correo(c["email"], f"✅ ¡Gracias por tu compra en UmiClean! Pedido #{ref}", f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#333">
      <div style="background:#0d2b52;padding:32px;text-align:center;border-radius:12px 12px 0 0">
        <h1 style="color:white;margin:0">UmiClean</h1>
        <p style="color:#00c9a7;margin:8px 0 0">Oxígeno Activo · Sin Cloro</p>
      </div>
      <div style="background:#f7f9fc;padding:32px;border-radius:0 0 12px 12px">
        <h2 style="color:#0d2b52;margin-top:0">¡Gracias por comprar, {c['nombre']}! 🎉</h2>
        <p>Recibimos tu pago y ya estamos preparando tu pedido.</p>
        <div style="background:#e8f8f4;border-left:4px solid #00c9a7;border-radius:0 10px 10px 0;padding:16px 18px;margin:20px 0">
          <p style="margin:0 0 6px;color:#0d2b52"><b>🚚 Tu pedido se enviará a:</b></p>
          <p style="margin:0 0 10px">{c['direccion']}, {c['ciudad']}</p>
          <p style="margin:0;color:#0d2b52"><b>📦 En máximo 2 días llegará tu pedido.</b></p>
        </div>
        <p style="color:#888;font-size:.85rem">Número de pedido <b style="color:#0d2b52">#{ref}</b> · {fecha}</p>
        {tabla_items(p['items'])}
        <p style="color:#0d2b52"><b>Envío:</b> {envio}<br><b>TOTAL PAGADO: ${cop(p['total'])} COP</b></p>
        <p style="font-size:.9rem;color:#555">¿Alguna duda? Escríbenos por WhatsApp al
          <a href="https://wa.me/{WHATSAPP_NUMBER}" style="color:#1a56a0;font-weight:bold">+57 310 696 6593</a>.</p>
        <p style="text-align:center;color:#888;font-size:.8rem;margin-top:24px">UmiClean · {CITY} · umiclean.co</p>
      </div>
    </div>""")

    # 2. A UmiClean
    wa = (f"https://wa.me/57{c['celular']}?text=" +
          urllib.request.quote(f"Hola {c['nombre']}, tu pedido UmiClean #{ref} fue confirmado y está en camino 🚚 ¡Gracias por tu compra!"))
    enviar_correo(CORREO_ADMIN, f"🛒 Nuevo pedido #{ref} — {c['nombre']} — ${cop(p['total'])} COP", f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:32px;border:2px solid #0d2b52;border-radius:12px">
      <h2 style="color:#0d2b52">🛒 Nuevo pedido PAGADO</h2>
      <p>Referencia: <b>{ref}</b> · {fecha} · Wompi: {p.get('transaction_id', '')}</p>
      <p>👤 {c['nombre']}<br>📱 {c['celular']}<br>✉️ {c['email']}<br>📍 {c['direccion']}, {c['ciudad']}</p>
      {tabla_items(p['items'])}
      <p style="font-size:1.1rem;color:#0d2b52"><b>TOTAL: ${cop(p['total'])} COP</b> · Envío: {envio}</p>
      <a href="{wa}" style="display:inline-block;background:#25d366;color:white;padding:14px 24px;border-radius:999px;font-weight:700;text-decoration:none">💬 Escribirle por WhatsApp</a>
    </div>""")


# =========================
# REPORTE DIARIO 9:00 PM — pedidos pagados pendientes por enviar
# =========================
HORA_REPORTE = 21  # 9 p. m. hora Colombia
# Clave para los botones "Marcar como enviado" del correo
ADMIN_KEY = os.environ.get("ADMIN_KEY") or WOMPI_INTEGRITY_SECRET
REPORTE_FILE = os.path.join(os.path.dirname(PEDIDOS_FILE) or ".", "ultimo_reporte.txt")
SITE_URL = os.environ.get("SITE_URL", "https://www.umiclean.co")


def firma_admin(texto):
    return hmac.new(ADMIN_KEY.encode(), texto.encode(), hashlib.sha256).hexdigest()[:24]


def pendientes_por_enviar():
    pedidos = cargar_pedidos()
    lista = [p for p in pedidos.values() if p.get("status") == "APPROVED" and not p.get("enviado")]
    return sorted(lista, key=lambda p: p.get("pagado") or p.get("creado", ""))


def enviar_reporte_diario():
    lista = pendientes_por_enviar()
    hoy = ahora().strftime("%d/%m/%Y")
    total = sum(p["total"] for p in lista)

    if not lista:
        cuerpo = """<div style="background:#e8f8f4;border-radius:12px;padding:28px;text-align:center">
          <p style="font-size:1.1rem;margin:0;color:#0d2b52"><b>✅ No hay pedidos pendientes por enviar.</b></p>
          <p style="margin:8px 0 0;color:#555">Todo está al día.</p></div>"""
    else:
        bloques = []
        for i, p in enumerate(lista, 1):
            c = p["cliente"]
            pagado = (p.get("pagado") or p.get("creado", ""))[:16].replace("T", " ")
            prods = "<br>".join(f"• {it['name']} x{it['qty']}" for it in p["items"])
            link = f"{SITE_URL}/admin/enviado/{p['ref']}?k={firma_admin(p['ref'])}"
            wa = f"https://wa.me/57{c['celular']}"
            bloques.append(f"""
            <div style="border:1px solid #d9e2ef;border-radius:12px;padding:18px 20px;margin:0 0 14px;background:white">
              <p style="margin:0 0 8px;color:#0d2b52;font-size:1.05rem"><b>{i}. {c['nombre']}</b>
                <span style="color:#888;font-size:.85rem;font-weight:normal"> · #{p['ref']} · pagado {pagado}</span></p>
              <p style="margin:0 0 4px">📍 <b>{c['direccion']}, {c['ciudad']}</b></p>
              <p style="margin:0 0 4px">📱 <a href="{wa}" style="color:#1a56a0">{c['celular']}</a> · ✉️ {c['email']}</p>
              <p style="margin:8px 0;color:#333">{prods}</p>
              <p style="margin:0 0 12px;color:#0d2b52"><b>${cop(p['total'])} COP</b>
                · Envío: {"GRATIS" if p['envio'] == 0 else "$" + cop(p['envio'])}</p>
              <a href="{link}" style="display:inline-block;background:#00c9a7;color:#0d2b52;padding:10px 18px;border-radius:999px;font-weight:bold;text-decoration:none">✅ Marcar como enviado</a>
            </div>""")
        cuerpo = "".join(bloques)

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;background:#f7f9fc;padding:24px;border-radius:12px;color:#333">
      <h2 style="color:#0d2b52;margin:0 0 4px">📦 Pedidos por enviar — {hoy}</h2>
      <p style="margin:0 0 20px;color:#555">{len(lista)} pedido(s) pagado(s) pendiente(s) · Total ${cop(total)} COP</p>
      {cuerpo}
      <p style="color:#888;font-size:.8rem;margin-top:20px">Cuando despaches un pedido, dale "Marcar como enviado" y no vuelve a salir en este correo.</p>
    </div>"""
    enviar_correo(CORREO_ADMIN, f"📦 UmiClean — {len(lista)} pedido(s) por enviar · {hoy}", html)


@app.route("/admin/enviado/<ref>")
def marcar_enviado(ref):
    if not hmac.compare_digest(request.args.get("k", ""), firma_admin(ref)):
        abort(403)
    with _lock:
        pedidos = cargar_pedidos()
        p = pedidos.get(ref)
        if not p:
            abort(404)
        p["enviado"] = ahora().isoformat(timespec="seconds")
        guardar_pedidos(pedidos)
    return (f"<div style='font-family:Arial;max-width:480px;margin:80px auto;text-align:center'>"
            f"<h2 style='color:#0d2b52'>✅ Pedido #{ref} marcado como enviado</h2>"
            f"<p>{p['cliente']['nombre']} · {p['cliente']['direccion']}, {p['cliente']['ciudad']}</p>"
            f"<p style='color:#888'>Ya no aparecerá en el reporte de las 9 p. m.</p></div>")


@app.route("/admin/reporte")
def reporte_manual():
    """Para probar: /admin/reporte?k=<firma de 'reporte'> manda el reporte ahora mismo."""
    if not hmac.compare_digest(request.args.get("k", ""), firma_admin("reporte")):
        abort(403)
    enviar_reporte_diario()
    return "Reporte enviado a " + CORREO_ADMIN


def _ya_se_envio(fecha):
    try:
        with open(REPORTE_FILE) as f:
            return f.read().strip() == fecha
    except Exception:
        return False


def _programador():
    """Revisa cada minuto; a las 9 p. m. (o apenas arranque si ya pasó la hora) manda el reporte una vez al día."""
    while True:
        try:
            t = ahora()
            fecha = t.strftime("%Y-%m-%d")
            if t.hour >= HORA_REPORTE and not _ya_se_envio(fecha):
                with open(REPORTE_FILE, "w") as f:
                    f.write(fecha)
                enviar_reporte_diario()
        except Exception as e:
            print(f"Error en reporte diario: {e}")
        time.sleep(60)


if os.environ.get("REPORTE_DIARIO", "1") == "1":
    threading.Thread(target=_programador, daemon=True).start()


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
