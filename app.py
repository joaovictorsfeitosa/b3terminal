from flask import Flask, request, jsonify
import json
import os

app = Flask(__name__)

DATA_FILE = "data.json"

# Criar arquivo se não existir
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"usuarios": {}}, f)

def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


@app.route("/register", methods=["POST"])
def register():
    data = load_data()
    body = request.json

    user = body.get("user")
    password = body.get("password")

    if user in data["usuarios"]:
        return jsonify({"error": "Usuário já existe"}), 400

    data["usuarios"][user] = {
        "senha": password,
        "carteira": []
    }

    save_data(data)
    return jsonify({"message": "Conta criada"})


@app.route("/login", methods=["POST"])
def login():
    data = load_data()
    body = request.json

    user = body.get("user")
    password = body.get("password")

    if user in data["usuarios"] and data["usuarios"][user]["senha"] == password:
        return jsonify({"message": "Login ok"})
    
    return jsonify({"error": "Login inválido"}), 401


@app.route("/add", methods=["POST"])
def add():
    data = load_data()
    body = request.json

    user = body.get("user")
    nome = body.get("nome")
    valor = body.get("valor")

    if user not in data["usuarios"]:
        return jsonify({"error": "Usuário não existe"}), 400

    data["usuarios"][user]["carteira"].append({
        "nome": nome,
        "valor": valor
    })

    save_data(data)
    return jsonify({"message": "Adicionado"})


@app.route("/carteira/<user>", methods=["GET"])
def carteira(user):
    data = load_data()

    if user not in data["usuarios"]:
        return jsonify({"error": "Usuário não existe"}), 400

    return jsonify(data["usuarios"][user]["carteira"])


if __name__ == "__main__":
    app.run(debug=True)
