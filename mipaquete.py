"""
Integración con Mipaquete (API v2): cotizar envíos y generar guías pagando con el saldo de Mipaquete.
Documentación: https://api.documentacion.mipaquete.com/

La configuración (apikey y datos del remitente) se guarda en un archivo JSON en el volumen de Railway,
para que no se pierda en cada deploy y se pueda cambiar desde el panel /admin/pedidos.
"""
import os, json, uuid, math, unicodedata, threading, urllib.request, urllib.error

API = os.environ.get("MIPAQUETE_API", "https://api-v2.mpr.mipaquete.com").rstrip("/")

# Peso aproximado por producto (kg) para cotizar. Ajusta si cambian los empaques.
PESO_PRODUCTO = {"oxigeno": 1.1, "kit": 0.6, "gel": 2.3}
PESO_COMBO = {"p1": 4.0, "p2": 3.4, "p3": 2.9, "p4": 4.0}
CAJA_CM = {"width": 25, "large": 35, "height": 15}  # medidas de la caja estándar

_lock = threading.Lock()
_ciudades = None


def _config_path(pedidos_file):
    return os.path.join(os.path.dirname(pedidos_file) or ".", "mipaquete.json")


def cargar_config(pedidos_file):
    try:
        with open(_config_path(pedidos_file), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_config(pedidos_file, cfg):
    p = _config_path(pedidos_file)
    with _lock:
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)


def _request(method, path, apikey=None, body=None):
    headers = {"Content-Type": "application/json", "session-tracker": str(uuid.uuid4())}
    if apikey:
        headers["apikey"] = apikey
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            txt = r.read().decode()
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        raise RuntimeError(f"Mipaquete respondió {e.code}: {txt[:300]}")
    try:
        return json.loads(txt)
    except ValueError:
        return txt


def generar_apikey(email, password):
    r = _request("POST", "/generateApiKey", body={"email": email, "password": password})
    if isinstance(r, str) and len(r) > 30:
        return r.strip('"')
    if isinstance(r, dict):
        for v in r.values():
            if isinstance(v, str) and len(v) > 30:
                return v
    raise RuntimeError(f"No se pudo obtener la apikey: {str(r)[:200]}")


def saldo(apikey):
    try:
        r = _request("GET", "/getUser", apikey)
        if isinstance(r, dict):
            return r.get("cash")
    except Exception:
        pass
    return None


def _norm(s):
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode().lower()
    for x in (",", ".", "d.c", "dc", "-"):
        s = s.replace(x, " ")
    return " ".join(s.split())


def buscar_dane(apikey, ciudad):
    """Devuelve (codigo_dane, nombre) para el texto de ciudad que escribió el cliente."""
    global _ciudades
    if _ciudades is None:
        r = _request("GET", "/getLocations", apikey)
        lista = r if isinstance(r, list) else (r.get("data") or r.get("locations") or []) if isinstance(r, dict) else []
        out = []
        for c in lista:
            if not isinstance(c, dict):
                continue
            code = next((str(v) for v in c.values() if isinstance(v, (str, int)) and str(v).isdigit() and len(str(v)) == 8), None)
            nombre = c.get("locationName") or c.get("name") or c.get("cityName") or ""
            depto = c.get("departmentOrStateName") or c.get("departmentName") or c.get("department") or ""
            if code and nombre:
                out.append((code, nombre, depto))
        _ciudades = out
    q = _norm(ciudad)
    if "bogota" in q:
        return "11001000", "Bogotá D.C."
    exactos = [c for c in _ciudades if _norm(c[1]) == q or _norm(f"{c[1]} {c[2]}") == q]
    parciales = exactos or [c for c in _ciudades if q.startswith(_norm(c[1])) or _norm(c[1]) in q]
    if parciales:
        c = sorted(parciales, key=lambda c: -len(c[1]))[0]
        return c[0], f"{c[1]} ({c[2]})" if c[2] else c[1]
    return None, None


def peso_pedido(items):
    kg = 0.0
    for it in items:
        tabla = PESO_PRODUCTO if it.get("type") == "product" else PESO_COMBO
        kg += tabla.get(it.get("key"), 1.0) * int(it.get("qty", 1))
    return max(1, math.ceil(kg))


def cotizar(apikey, origen, destino, items, valor):
    body = {"originLocationCode": origen, "destinyLocationCode": destino,
            "height": CAJA_CM["height"], "width": CAJA_CM["width"], "length": CAJA_CM["large"],
            "weight": peso_pedido(items), "quantity": 1, "declaredValue": int(valor), "saleValue": 0}
    r = _request("POST", "/quoteShipping", apikey, body)
    if not isinstance(r, list):
        raise RuntimeError(f"Respuesta inesperada al cotizar: {str(r)[:200]}")
    opciones = [{"empresa": o.get("deliveryCompanyName"), "empresa_id": o.get("deliveryCompanyId"),
                 "costo": o.get("shippingCost"), "horas": round((o.get("shippingTime") or 0) / 60),
                 "solo_oficina": o.get("singleOfficeDelivery"), "score": o.get("score")} for o in r]
    return sorted([o for o in opciones if o["costo"]], key=lambda o: o["costo"])


def _partir_nombre(nombre):
    partes = str(nombre).split()
    return (partes[0], " ".join(partes[1:]) or partes[0]) if partes else ("Cliente", "UmiClean")


def crear_guia(apikey, cfg, pedido, empresa_id, destino, cedula=""):
    s, c = cfg.get("remitente", {}), pedido["cliente"]
    nombre, apellido = _partir_nombre(c["nombre"])
    resumen = ", ".join(f"{it['qty']}x {it['name']}" for it in pedido["items"])[:120]
    receiver = {"name": nombre, "surname": apellido, "email": c["email"], "prefix": "+57",
                "cellPhone": "".join(ch for ch in c["celular"] if ch.isdigit())[-10:],
                "destinationAddress": c["direccion"]}
    if cedula:
        receiver.update({"nit": cedula, "nitType": "CC"})
    body = {
        "sender": {"name": s.get("nombre", ""), "surname": s.get("apellido", ""), "cellPhone": s.get("celular", ""),
                   "prefix": "+57", "email": s.get("email", ""), "pickupAddress": s.get("direccion", ""),
                   "nit": s.get("nit", ""), "nitType": s.get("tipo_nit", "CC")},
        "receiver": receiver,
        "productInformation": {"quantity": 1, "width": CAJA_CM["width"], "large": CAJA_CM["large"],
                               "height": CAJA_CM["height"], "weight": peso_pedido(pedido["items"]),
                               "forbiddenProduct": True, "productReference": "UmiClean",
                               "declaredValue": int(pedido["subtotal"])},
        "locate": {"originDaneCode": cfg.get("origen_dane", "11001000"), "destinyDaneCode": destino},
        "channel": "umiclean.co",
        "deliveryCompany": empresa_id,
        "description": resumen,
        "comments": f"Pedido {pedido['ref']}",
        "paymentType": 101,          # 101 = pagar con el saldo de Mipaquete
        "valueCollection": 0,
        "requestPickup": bool(cfg.get("recogida", True)),
        "adminTransactionData": {"saleValue": 0},
    }
    r = _request("POST", "/createSending", apikey, body)
    if isinstance(r, dict) and r.get("mpCode"):
        return r["mpCode"], r.get("message", "")
    raise RuntimeError(f"No se pudo generar la guía: {str(r)[:300]}")
