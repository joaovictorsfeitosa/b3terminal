# 🔐 Sistema de Autenticação — B3 Terminal

## Visão Geral

O sistema implementado usa:
- **SQLite** como banco de dados local (sem custo, funciona no Render)
- **PBKDF2-SHA256** com 310.000 iterações para hash de senhas (padrão NIST)
- **JWT-like tokens** aleatórios com 48 bytes (armazenados como hash SHA-256)
- **Rate limiting** embutido: máx. 10 tentativas por 15 min por IP
- **Device fingerprinting** leve (sem libs externas)

---

## Endpoints da API

### Autenticação
| Método | Rota | Descrição |
|--------|------|-----------|
| POST | `/api/auth/register` | Cria conta |
| POST | `/api/auth/login` | Login → retorna token |
| POST | `/api/auth/logout` | Invalida token atual |
| GET  | `/api/auth/me` | Info do usuário logado |
| POST | `/api/auth/change-password` | Altera senha |

### Dados do Usuário (sincronizados)
| Método | Rota | Descrição |
|--------|------|-----------|
| GET  | `/api/user/data/{key}` | Lê dado do usuário |
| POST | `/api/user/data/{key}` | Salva dado do usuário |
| GET  | `/api/user/devices` | Lista dispositivos do usuário |

### Painel Admin (apenas admin)
| Método | Rota | Descrição |
|--------|------|-----------|
| GET  | `/api/admin/stats` | Estatísticas gerais |
| GET  | `/api/admin/users` | Lista todos os usuários |
| GET  | `/api/admin/devices` | Lista todos os dispositivos |
| POST | `/api/admin/user/{id}/block` | Bloqueia/desbloqueia usuário |
| POST | `/api/admin/device/{id}/block` | Bloqueia/desbloqueia dispositivo |

---

## Configuração no Render

### Variáveis de Ambiente (OBRIGATÓRIO configurar)

No painel do Render → seu serviço → **Environment**:

| Variável | Valor | Descrição |
|----------|-------|-----------|
| `SECRET_KEY` | (gere 64 chars aleatórios) | Chave de segurança |
| `ADMIN_USER` | `seuadmin` | Seu usuário admin |
| `ADMIN_PASS` | `SuaSenhaForte@2025!` | Senha do admin |
| `DB_PATH` | `/data/b3terminal.db` | Caminho do banco |

### Gerar SECRET_KEY
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Persistência do banco no Render

O Render tem sistema de arquivos **efêmero** — o banco é perdido ao reiniciar
sem um disco persistente.

**Opção 1 — Disk persistente (recomendado, plano pago):**
No Render → seu serviço → Disks → Add Disk:
- Mount Path: `/data`
- Size: 1 GB
- Então: `DB_PATH=/data/b3terminal.db`

**Opção 2 — Gratuito (banco reseta ao reiniciar):**
Deixe `DB_PATH=b3terminal.db` — o admin é recriado automaticamente a cada boot.
Os dados dos usuários são perdidos ao reiniciar. Aceitável para testes.

**Opção 3 — PostgreSQL (recomendado para produção):**
O código pode ser adaptado para usar psycopg2 + PostgreSQL (Render oferece
banco PostgreSQL gratuito por 90 dias).

---

## Segurança Implementada

### ✅ Proteções ativas
- **Senhas hasheadas** com PBKDF2-SHA256 + salt único de 32 bytes
- **Tokens opacos** — o token real nunca é armazenado no servidor (só o hash)
- **Rate limiting** — 10 tentativas de login/registro por 15 min por IP
- **Tokens expiram** automaticamente em 7 dias
- **Sessões revogadas** ao trocar senha ou bloquear usuário
- **Comparação de hash segura** com `hmac.compare_digest` (anti-timing attack)
- **Dispositivos bloqueáveis** individualmente pelo admin
- **Log de todos os logins** (sucesso e falha)

### ✅ Proteção CSRF
O sistema usa tokens Bearer no header `Authorization`, não cookies,
portanto é imune a CSRF por design.

### ✅ Proteção XSS
Os dados são trafegados como JSON — o template não injeta dados do usuário
no HTML sem sanitização.

### ⚠️ Recomendações adicionais para produção
- Use HTTPS (o Render já fornece)
- Configure `ADMIN_PASS` com senha forte (mínimo 12 chars, símbolos)
- Faça backup regular do banco de dados
- Monitore o log de logins com frequência

---

## Login de Admin

Após o deploy, o admin é criado automaticamente com:
- Usuário: valor de `ADMIN_USER` (padrão: `admin`)
- Senha: valor de `ADMIN_PASS` (padrão: `B3Terminal@Admin2025!`)

**Troque a senha padrão imediatamente após o primeiro login!**

Acesso ao painel: faça login → clique no seu nome → "Painel Admin"

---

## Estrutura do Banco de Dados

```sql
users       — contas de usuário
sessions    — tokens ativos
devices     — dispositivos identificados por fingerprint
user_data   — carteira e dados sincronizados por usuário
login_log   — histórico de tentativas de login
```
