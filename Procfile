# 🚀 PUBLICAR O B3 TERMINAL ONLINE — PASSO A PASSO

Tempo estimado: 10 minutos
Resultado: link permanente e gratuito acessível por qualquer pessoa

---

## ETAPA 1 — Criar conta no GitHub (grátis)

1. Acesse: https://github.com/signup
2. Preencha: usuário, e-mail e senha
3. Confirme o e-mail que chegará na sua caixa

---

## ETAPA 2 — Criar repositório no GitHub

1. Acesse: https://github.com/new
2. Preencha:
   - Repository name: b3terminal
   - Marque: Public (obrigatório para o Render gratuito)
3. Clique em "Create repository"
4. Na próxima tela, clique em "uploading an existing file"
5. Arraste TODOS esses arquivos para a área de upload:
   ✅ app.py
   ✅ requirements.txt
   ✅ Procfile
   ✅ render.yaml
   ✅ runtime.txt
   ✅ A PASTA templates (com o index.html dentro)
6. Clique em "Commit changes" (botão verde)

---

## ETAPA 3 — Criar conta no Render (grátis)

1. Acesse: https://render.com
2. Clique em "Get Started for Free"
3. Clique em "Continue with GitHub" e autorize

---

## ETAPA 4 — Publicar o site no Render

1. No Render, clique em "New +" (canto superior direito)
2. Escolha "Web Service"
3. Clique em "Connect" no repositório b3terminal
4. Configure assim:
   - Name: b3-terminal
   - Region: Frankfurt (EU) — mais próximo do Brasil
   - Branch: main
   - Runtime: Python 3
   - Build Command: pip install -r requirements.txt
   - Start Command: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
   - Instance Type: Free
5. Clique em "Create Web Service"

---

## ETAPA 5 — Aguardar o deploy (3-5 minutos)

O Render vai mostrar os logs de instalação.
Quando aparecer "Your service is live" ou a bolinha ficar verde:

🎉 SEU SITE ESTÁ NO AR!

O link será algo como:
https://b3-terminal.onrender.com

---

## ⚠️ Aviso importante

O plano gratuito do Render "hiberna" após 15 minutos sem acesso.
Quando alguém acessar depois disso, pode demorar ~30 segundos
para carregar a primeira vez. Depois fica rápido normalmente.

Para evitar isso, cadastre o link no UptimeRobot (gratuito):
https://uptimerobot.com
Ele "pinga" o site a cada 5 minutos e mantém ativo.

---

## 🔄 Como atualizar o site depois

1. No GitHub, abra o arquivo que quer alterar
2. Clique no lápis ✏️ para editar
3. Faça a alteração e clique em "Commit changes"
4. O Render detecta e republica automaticamente em ~2 min
