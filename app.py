import os
import json
import shutil
import subprocess
import logging
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, abort
from werkzeug.middleware.proxy_fix import ProxyFix

# Firebase Admin (backend token verification)
import firebase_admin
from firebase_admin import credentials, auth

# Linux file lock (Render runs on Linux)
import fcntl


# ----------------------------
# Config
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent

# Persistent output directory:
# - Default keeps your current structure: ./output
# - In Render, you can point it to a mounted disk path via OUTPUT_DIR
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()

# HTML file to serve (your current file)
INDEX_HTML = os.environ.get("INDEX_HTML", "NutriScore.html")

# Yazio credentials (DO NOT hardcode)
YAZIO_EMAIL = os.environ.get("YAZIO_EMAIL")
YAZIO_PASSWORD = os.environ.get("YAZIO_PASSWORD")  # yazio-exporter expects password in README examples [6](https://github.com/aleksandr-bogdanov/yazio-exporter)

# Optional: restrict who can trigger import by email allowlist (comma-separated)
ALLOWED_EMAILS = set(
    e.strip().lower()
    for e in os.environ.get("ALLOWED_EMAILS", "").split(",")
    if e.strip()
)

# Optional: if true, require Firebase AppCheck too (advanced). Default off.
REQUIRE_APPCHECK = os.environ.get("REQUIRE_APPCHECK", "0") == "1"

# Import lockfile
LOCKFILE = OUTPUT_DIR / ".import.lock"

# Subprocess safety
IMPORT_TIMEOUT_SECONDS = int(os.environ.get("IMPORT_TIMEOUT_SECONDS", "600"))  # 10 min default

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


# ----------------------------
# App init
# ----------------------------
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # behind Render proxy

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nutritrack")

# Ensure output dir exists
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Keep a stable "output" path for yazio-exporter (it writes to ./output by default) [6](https://github.com/aleksandr-bogdanov/yazio-exporter)
# If OUTPUT_DIR is different than BASE_DIR/output, we create a symlink BASE_DIR/output -> OUTPUT_DIR
try:
    default_output = BASE_DIR / "output"
    if default_output.exists() or default_output.is_symlink():
        # If it's a dir and it's not our OUTPUT_DIR, leave it as-is (you might already manage it)
        pass
    else:
        if str(OUTPUT_DIR) != str(default_output):
            default_output.symlink_to(OUTPUT_DIR, target_is_directory=True)
except Exception as e:
    logger.warning("Could not create output symlink: %s", e)


# ----------------------------
# Firebase Admin initialization
# ----------------------------
def init_firebase_admin():
    """
    Secure initialization for Firebase Admin:
    - Preferred: FIREBASE_SERVICE_ACCOUNT_JSON env var (full JSON content)
    - Alternative: GOOGLE_APPLICATION_CREDENTIALS env var (path in container)
    """
    if firebase_admin._apps:
        return

    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        try:
            info = json.loads(sa_json)
            cred = credentials.Certificate(info)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin initialized from FIREBASE_SERVICE_ACCOUNT_JSON.")
            return
        except Exception as e:
            logger.error("Failed to init Firebase Admin from JSON env var: %s", e)
            raise

    # If GOOGLE_APPLICATION_CREDENTIALS is set, Admin SDK can use it
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        firebase_admin.initialize_app()
        logger.info("Firebase Admin initialized from GOOGLE_APPLICATION_CREDENTIALS.")
        return

    # No credentials configured -> in production we should fail closed
    logger.error(
        "Firebase Admin not configured. Set FIREBASE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS."
    )
    raise RuntimeError("Firebase Admin not configured")


# ----------------------------
# Security helpers
# ----------------------------
def _get_bearer_token():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    return h.split("Bearer ", 1)[1].strip()


def require_firebase_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Initialize admin SDK (fails closed if missing)
        init_firebase_admin()

        token = _get_bearer_token()
        if not token:
            return jsonify({"status": "unauthorized", "message": "Missing Bearer token"}), 401

        try:
            decoded = auth.verify_id_token(token)  # verifies signature/expiry [2](https://firebase.google.com/docs/auth/admin/verify-id-tokens)
        except Exception:
            return jsonify({"status": "unauthorized", "message": "Invalid token"}), 401

        # Optional allowlist
        if ALLOWED_EMAILS:
            email = (decoded.get("email") or "").lower()
            if email not in ALLOWED_EMAILS:
                return jsonify({"status": "forbidden", "message": "User not allowed"}), 403

        # Attach user to request context if you need it
        request.user = decoded
        return fn(*args, **kwargs)

    return wrapper


@app.after_request
def add_security_headers(resp):
    # Basic hardening (safe defaults)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # If you later host under HTTPS only (Render does), HSTS is ok:
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


# ----------------------------
# Core logic (based on your existing code) [4](https://jespac0-my.sharepoint.com/personal/alex_jespac_com/Documents/Archivos%20de%20chat%20de%20Microsoft%C2%A0Copilot/app.py)
# ----------------------------
def procesar_jsons():
    try:
        days_path = OUTPUT_DIR / "days.json"
        nutrients_path = OUTPUT_DIR / "nutrients.json"
        products_path = OUTPUT_DIR / "products.json"

        if not (days_path.exists() and nutrients_path.exists() and products_path.exists()):
            return None

        with days_path.open("r", encoding="utf-8") as f:
            days_data = json.load(f)

        with nutrients_path.open("r", encoding="utf-8") as f:
            nutrients_data = json.load(f)

        with products_path.open("r", encoding="utf-8") as f:
            products_data = json.load(f)

    except Exception as e:
        logger.error("Error reading JSON files: %s", e)
        return None

    productos_dict = products_data.get("products", {}) if isinstance(products_data, dict) else {}

    fechas = sorted(list(days_data.keys()))
    kcals, carbs, prots, fats, fasts = [], [], [], [], []
    meals_data_export = {}

    traductor_comidas = {
        "breakfast": "Desayuno",
        "lunch": "Almuerzo",
        "dinner": "Cena",
        "snack": "Snack",
    }

    for date in fechas:
        daily_k = daily_c = daily_p = daily_f = 0
        comidas_registradas = 0

        meals_summary = (
            days_data.get(date, {})
            .get("daily_summary", {})
            .get("meals", {})
        )

        for meal_name, meal_data in meals_summary.items():
            n = meal_data.get("nutrients", {}) if isinstance(meal_data, dict) else {}
            e = n.get("energy.energy", 0)
            if e and e > 0:
                comidas_registradas += 1
                daily_k += e
                daily_c += n.get("nutrient.carb", 0)
                daily_p += n.get("nutrient.protein", 0)
                daily_f += n.get("nutrient.fat", 0)

        kcals.append(round(daily_k))
        carbs.append(round(daily_c))
        prots.append(round(daily_p))
        fats.append(round(daily_f))

        es_omad = comidas_registradas <= 1
        fasts.append(24 if es_omad else 16)

        dia_meals = {}
        consumed_list = (
            days_data.get(date, {})
            .get("consumed", {})
            .get("products", [])
        )

        for item in consumed_list:
            daytime_raw = item.get("daytime", "snack")
            daytime_es = traductor_comidas.get(daytime_raw, "Comida")

            prod_id = item.get("product_id")
            time_str = ""
            if item.get("date"):
                parts = item["date"].split(" ")
                if len(parts) > 1:
                    time_str = parts[1][:5]

            prod_name = (
                productos_dict.get(prod_id, {})
                .get("name", "Alimento")
                if isinstance(productos_dict, dict)
                else "Alimento"
            )

            if es_omad:
                daytime_es = "Única Comida (OMAD)"
                icono = "🔥"
            else:
                icono = "☀️" if daytime_raw == "breakfast" else "🌤️" if daytime_raw == "lunch" else "🌙"

            if daytime_es not in dia_meals:
                dia_meals[daytime_es] = {
                    "time": time_str,
                    "type": daytime_es,
                    "icon": icono,
                    "items": [],
                }

            if prod_name not in dia_meals[daytime_es]["items"]:
                dia_meals[daytime_es]["items"].append(prod_name)

        meals_data_export[date] = list(dia_meals.values())

    meses = {"01": "Ene", "02": "Feb", "03": "Mar", "04": "Abr", "05": "May", "06": "Jun",
             "07": "Jul", "08": "Ago", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dic"}
    fechas_formateadas = [f"{d[-2:]} {meses.get(d[5:7], d[5:7])}" for d in fechas]

    return {
        "fechas": fechas_formateadas,
        "isoDates": fechas,
        "kcals": kcals,
        "carbs": carbs,
        "prots": prots,
        "fats": fats,
        "fasts": fasts,
        "rawMicros": nutrients_data,
        "mealsData": meals_data_export,
    }


def _run_yazio_export():
    """
    Runs 'yazio-exporter export-all EMAIL PASSWORD' in a safe way.
    The exporter writes to ./output by default (relative) [6](https://github.com/aleksandr-bogdanov/yazio-exporter)
    """
    if not YAZIO_EMAIL or not YAZIO_PASSWORD:
        raise RuntimeError("Missing YAZIO_EMAIL or YAZIO_PASSWORD env vars")

    exe = shutil.which("yazio-exporter")
    if not exe:
        raise RuntimeError("yazio-exporter executable not found. Ensure it's installed in requirements.txt")

    cmd = [exe, "export-all", YAZIO_EMAIL, YAZIO_PASSWORD]

    # Use a lock to avoid concurrent imports corrupting files
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCKFILE.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)

        logger.info("Starting Yazio export...")
        subprocess.run(
            cmd,
            cwd=str(BASE_DIR),            # ensures ./output resolves correctly
            check=True,
            timeout=IMPORT_TIMEOUT_SECONDS
        )
        logger.info("Yazio export completed.")


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    # Serve your static HTML (same as your current send_file approach) [4](https://jespac0-my.sharepoint.com/personal/alex_jespac_com/Documents/Archivos%20de%20chat%20de%20Microsoft%C2%A0Copilot/app.py)[5](https://jespac0-my.sharepoint.com/personal/alex_jespac_com/Documents/Archivos%20de%20chat%20de%20Microsoft%C2%A0Copilot/NutriScore.html)
    return send_from_directory(str(BASE_DIR), INDEX_HTML)


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/api/datos")
def get_datos():
    data = procesar_jsons()
    if data:
        return jsonify({"status": "success", "data": data})
    return jsonify({"status": "empty"})


@app.post("/api/importar")
@require_firebase_auth
def importar_datos():
    """
    Protected endpoint:
    - Requires Firebase ID token in Authorization header (Bearer ...)
    - Runs yazio-exporter on the server (Render)
    """
    try:
        _run_yazio_export()
        data = procesar_jsons()
        if data:
            return jsonify({"status": "success", "data": data})
        return jsonify({"status": "success", "data": None, "message": "Export done but no JSON found yet"})
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Import timeout"}), 504
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "message": f"Import failed (exit {e.returncode})"}), 500
    except Exception as e:
        logger.exception("Import error")
        return jsonify({"status": "error", "message": str(e)}), 500