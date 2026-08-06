# Dashboard de Funil de Tráfego — Meta Ads

Dashboard estático que cruza **2 planilhas Google** (métricas de anúncios do Meta + lista
de vendas), monta o funil completo (Investimento → Impressões → Cliques → Page views →
Checkouts → Vendas) e calcula todas as métricas (CPM, CTR, CPC, CPL, CAC, conversão, ROAS)
**com o gasto já com imposto ×1,1385**.

- **100% na nuvem**: o build roda no GitHub Actions e publica no GitHub Pages. Não depende do seu PC.
- **Somente leitura**: nunca escreve nada nas planilhas.
- **Sem dependências**: `build.py` usa só a biblioteca padrão do Python.
- **Cache-bust**: cada build carimba a página com um id + timestamp, então o navegador nunca serve dados velhos.

---

## Como funciona

```
cron-job.org  ──POST──►  GitHub API (repository_dispatch)
                              │
                              ▼
                    GitHub Actions (build.yml)
                              │  python build.py  → dist/{index.html, data.json}
                              ▼
                    GitHub Pages  →  URL pública
```

`build.py` baixa as duas planilhas como CSV, normaliza as UTMs, atribui cada venda à
campanha/conjunto/anúncio de origem (do mais específico ao menos) e gera `dist/data.json`.
O `index.html` lê esse JSON e desenha tudo no navegador.

---

## Publicar (passo a passo — ~4 minutos)

> Você faz isto uma única vez. Todos os builds seguintes são automáticos.

### 1. Crie um repositório e suba o código

No seu terminal, dentro da pasta `meta-funnel-dashboard`:

```bash
git init
git add .
git commit -m "Dashboard de funil Meta Ads"
git branch -M main
```

Crie um repositório **novo e vazio** em https://github.com/new (pode ser público ou privado;
o nome sugerido é `meta-funnel-dashboard`). Depois:

```bash
git remote add origin https://github.com/SEU-USUARIO/meta-funnel-dashboard.git
git push -u origin main
```

O `git push` vai pedir seu login do GitHub — use seu usuário e um **token novo** (o antigo
que você tinha em mãos deve ser revogado). É aqui, na sua máquina, que o token é usado —
ele não passa por lugar nenhum além do seu Git local.

### 2. Ative o GitHub Pages

No repositório: **Settings → Pages → Build and deployment → Source: `GitHub Actions`**.
(Não escolha "Deploy from a branch" — o workflow já cuida do deploy.)

### 3. Rode o primeiro build

Aba **Actions → "Build e publicar dashboard" → Run workflow**.
Em ~1 minuto a dashboard estará no ar em:

```
https://SEU-USUARIO.github.io/meta-funnel-dashboard/
```

---

## Automação a cada 3h (cron-job.org)

O workflow aceita ser disparado externamente via `repository_dispatch`. Configure um job em
https://cron-job.org com estes valores:

| Campo | Valor |
|---|---|
| **URL** | `https://api.github.com/repos/SEU-USUARIO/meta-funnel-dashboard/dispatches` |
| **Método / Request method** | `POST` |
| **Schedule** | a cada 3 horas (`0 */3 * * *`) |

**Headers** (aba *Advanced → Headers*):

```
Accept: application/vnd.github+json
Authorization: Bearer SEU_TOKEN_NOVO
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

**Body / Request body:**

```json
{"event_type":"rebuild"}
```

O token do header precisa de permissão para disparar workflows:
- **Token clássico**: escopo `repo` (ou `public_repo` se o repositório for público).
- **Token fine-grained**: acesso a este repositório + permissão **Contents: Read and write**.

> Rede de segurança: o `build.yml` também tem um `schedule` interno do GitHub a cada 3h. Ele é
> "best effort" (a fila do GitHub às vezes atrasa ou pula), por isso o cron-job.org é o gatilho
> principal e confiável.

---

## Configuração

Tudo que muda de campanha para campanha está em [`config.json`](config.json):

- `tax_multiplier`: o imposto. Está em `1.1385` (Meta Ads). **Se um dia a dashboard for de
  Google Ads, troque para `1.0`** — nenhum imposto será aplicado.
- `ads` / `sales`: os IDs das planilhas, o nome da aba de vendas e o mapeamento de colunas.
  Se você renomear uma coluna na planilha, ajuste aqui.
- `approved_status`: quais status contam como venda (`APPROVED`, `PAGO`, etc.).

As planilhas precisam continuar compartilhadas como **"qualquer pessoa com o link pode ver"**
para o build conseguir lê-las.

---

## Rodar localmente (opcional)

```bash
python3 build.py
python3 -m http.server 8000 --directory dist
# abra http://localhost:8000
```
