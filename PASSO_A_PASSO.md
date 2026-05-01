# 🚀 Como publicar o B3 Terminal online (grátis)

## Opção 1 — Render.com (RECOMENDADO — link permanente grátis)

### Passo 1 — Crie uma conta gratuita
Acesse: https://render.com → Sign Up → Continue with GitHub

### Passo 2 — Suba o código no GitHub
1. Acesse: https://github.com/new
2. Crie um repositório chamado: b3terminal
3. Marque como Público
4. Clique em "Create repository"
5. Na página seguinte, clique em "uploading an existing file"
6. Arraste TODOS os arquivos desta pasta (exceto DEPLOY.md)
7. Clique em "Commit changes"

### Passo 3 — Deploy no Render
1. No Render, clique em "New +" → "Web Service"
2. Conecte o repositório b3terminal do GitHub
3. Configure assim:
   - Name: b3-terminal
   - Region: Ohio (US East)
   - Branch: main
   - Runtime: Python 3
   - Build Command: pip install -r requirements.txt
   - Start Command: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
4. Plano: FREE
5. Clique em "Create Web Service"

### Passo 4 — Aguarde o deploy (3-5 min)
O Render vai te dar um link assim:
https://b3-terminal.onrender.com

✅ Qualquer pessoa no mundo pode acessar esse link!

---

## Opção 2 — Railway.app (também grátis)

1. Acesse: https://railway.app
2. Login com GitHub
3. New Project → Deploy from GitHub repo
4. Selecione o repositório b3terminal
5. Em 2 minutos o link estará pronto

---

## Opção 3 — Rodar só no seu PC

Dê duplo clique em: start.bat (Windows) ou ./start.sh (Mac/Linux)
Acesse: http://localhost:5000

---

## ⚠️ Aviso importante sobre o Render grátis

O plano gratuito do Render hiberna o servidor após 15min sem uso.
Na primeira visita depois disso, pode demorar ~30 segundos para carregar.
Após isso, funciona normalmente.

Para evitar isso, use o UptimeRobot (gratuito) para "pingar" o site a cada 10 min:
https://uptimerobot.com
