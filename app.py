from flask import Flask, jsonify, send_file
import subprocess
import json
import sys
import os

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route('/')
def home():
    return send_file(os.path.join(BASE_DIR, 'NutriScore.html'))

def procesar_jsons():
    output_dir = os.path.join(BASE_DIR, 'output')
    try:
        with open(os.path.join(output_dir, 'days.json'), 'r', encoding='utf-8') as f:
            days_data = json.load(f)
        with open(os.path.join(output_dir, 'nutrients.json'), 'r', encoding='utf-8') as f:
            nutrients_data = json.load(f)
        # Cargamos products.json para traducir los IDs a nombres reales
        with open(os.path.join(output_dir, 'products.json'), 'r', encoding='utf-8') as f:
            products_data = json.load(f)
    except FileNotFoundError:
        return None 

    productos_dict = products_data.get('products', {})
    fechas = sorted(list(days_data.keys()))
    
    kcals, carbs, prots, fats, fasts = [], [], [], [], []
    meals_data_export = {} # Diccionario para guardar los alimentos reales
    
    traductor_comidas = {
        "breakfast": "Desayuno",
        "lunch": "Almuerzo",
        "dinner": "Cena",
        "snack": "Snack"
    }

    for date in fechas:
        daily_k = daily_c = daily_p = daily_f = 0
        comidas_registradas = 0
        
        # 1. Extracción de Macros y Calorías
        meals_summary = days_data[date].get('daily_summary', {}).get('meals', {})
        for meal_name, meal_data in meals_summary.items():
            n = meal_data.get('nutrients', {})
            e = n.get('energy.energy', 0)
            if e > 0:
                comidas_registradas += 1
                daily_k += e
                daily_c += n.get('nutrient.carb', 0)
                daily_p += n.get('nutrient.protein', 0)
                daily_f += n.get('nutrient.fat', 0)
        
        kcals.append(round(daily_k))
        carbs.append(round(daily_c))
        prots.append(round(daily_p))
        fats.append(round(daily_f))
        
        # Lógica de Ayuno
        es_omad = comidas_registradas <= 1
        fasts.append(24 if es_omad else 16)

        # 2. Extracción de los Alimentos Reales consumidos ese día
        dia_meals = {}
        consumed_list = days_data[date].get('consumed', {}).get('products', [])
        
        for item in consumed_list:
            daytime_raw = item.get('daytime', 'snack')
            daytime_es = traductor_comidas.get(daytime_raw, "Comida")
            prod_id = item.get('product_id')
            
            # Formatear la hora (ej. de "2026-02-18 13:21:26" a "13:21")
            time_str = ""
            if item.get('date'):
                time_str = item['date'].split(' ')[1][:5]
            
            # Obtener nombre real del producto
            prod_name = productos_dict.get(prod_id, {}).get('name', 'Alimento')
            
            if daytime_es not in dia_meals:
                icono = "☀️" if daytime_raw == "breakfast" else "🌤️" if daytime_raw == "lunch" else "🌙"
                if es_omad: 
                    daytime_es = "Única Comida (OMAD)"
                    icono = "🔥"
                    
                dia_meals[daytime_es] = {
                    "time": time_str,
                    "type": daytime_es,
                    "icon": icono,
                    "items": []
                }
            
            # Evitar duplicados exactos en la misma comida
            if prod_name not in dia_meals[daytime_es]["items"]:
                dia_meals[daytime_es]["items"].append(prod_name)

        meals_data_export[date] = list(dia_meals.values())

    meses = {"01":"Ene","02":"Feb","03":"Mar","04":"Abr","05":"May","06":"Jun","07":"Jul","08":"Ago","09":"Sep","10":"Oct","11":"Nov","12":"Dic"}
    fechas_formateadas = [f"{d[-2:]} {meses[d[5:7]]}" for d in fechas]

    return {
        "fechas": fechas_formateadas,
        "isoDates": fechas,
        "kcals": kcals, "carbs": carbs, "prots": prots, "fats": fats, "fasts": fasts,
        "rawMicros": nutrients_data,
        "mealsData": meals_data_export # <- ¡AQUÍ MANDAMOS LAS COMIDAS A LA WEB!
    }

@app.route('/api/datos', methods=['GET'])
def get_datos():
    data = procesar_jsons()
    if data: return jsonify({"status": "success", "data": data})
    return jsonify({"status": "empty"})

@app.route('/api/importar', methods=['POST'])
def importar_datos():
    try:
        print("Iniciando descarga desde Yazio...")
        subprocess.run("yazio-exporter export-all alex@jespac.com gupbot-tuzham-gutHy3", shell=True, cwd=BASE_DIR, check=True)
        data = procesar_jsons()
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)