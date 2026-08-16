# sescbot

Monitor de liberação de datas no SESC PR (Reserva Online). Roda no GitHub
Actions a cada 15 min, consulta o site e avisa no Telegram quando qualquer
data sai da lista de "indisponíveis" e abre para reserva — inclusive dia de
semana.

## Como funciona

O site do SESC PR expõe duas listas de datas:

- `DatasIndisponiveis` — ainda não liberadas para reserva (vão liberar em
  algum momento).
- `DatasBloqueio` — unidade fechada, não vai liberar.

Uma data que **não está em nenhuma das duas** está aberta para reserva agora.
O evento monitorado é: qualquer data que estava em `DatasIndisponiveis` e
deixou de estar (sem cair em `DatasBloqueio`), seja dia de semana ou fim de
semana.

O script guarda um snapshot em `estado.json` a cada execução e compara com o
snapshot anterior. A primeira execução só grava a baseline, sem notificar.
Quando alguma data nova abre, a notificação lista **todas** as datas abertas
no momento (não só a que mudou), do dia de hoje até a fronteira do
calendário, cada uma com o dia da semana — assim fica claro o que é fim de
semana e o que não é.

Datas além da última data publicada nas duas listas (a "fronteira do
calendário") são ignoradas — não são "livres", é só o que o site ainda não
publicou.

## Uso local

```bash
pip install -r requirements.txt
cp .env.example .env   # preencha TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID
set -a; source .env; set +a

python sesc_watch.py            # roda uma vez
python sesc_watch.py --loop 600 # roda em loop, a cada 10 min
python sesc_watch.py --status   # só mostra a situação, não grava nem notifica
python sesc_watch.py --reset    # apaga a baseline
```

Antes de tudo, **abra uma conversa com o bot no Telegram** (mande `/start` ou
qualquer mensagem) — sem isso o bot não tem permissão para enviar mensagens
para o seu chat.

## Comandos do bot

Como o GitHub Actions não mantém processo ligado, os comandos não usam
webhook nem long polling: a cada execução (a cada 15 min), o script chama
`getUpdates` do Telegram com o offset salvo em `estado.json` e responde aos
comandos acumulados desde a última rodada. Latência de até 15 min é normal.

- `/status` — unidade monitorada, última verificação, fronteira do
  calendário, total de datas abertas agora (e quantas são de FDS), e total
  de datas bloqueadas.
- `/fila` — dias de FDS ainda em `DatasIndisponiveis`, agrupados por mês.

## Configuração (variáveis de ambiente)

| Variável | Default | Descrição |
|---|---|---|
| `SESC_COD_MEIO_HOSPEDAGEM` | `34` | Código da unidade no site |
| `SESC_NOME_UNIDADE` | `unidade 34` | Nome exibido nas mensagens |
| `SESC_STATE_FILE` | `estado.json` | Caminho do snapshot de estado |
| `TELEGRAM_BOT_TOKEN` | — | Token do bot (via @BotFather) |
| `TELEGRAM_CHAT_ID` | — | Chat para onde notificar |

## GitHub Actions

O workflow em `.github/workflows/monitor.yml` roda a cada 15 min
(`schedule`) e também pode ser disparado manualmente (`workflow_dispatch`,
com opção de resetar a baseline). Ele commita o `estado.json` de volta ao
repo ao final de cada execução, só se algo mudou.

Configure em **Settings → Secrets and variables → Actions**:

- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Variables (opcionais): `SESC_COD_MEIO_HOSPEDAGEM`, `SESC_NOME_UNIDADE`.

Use um repositório **público**: no privado, Actions grátis dá 2.000
min/mês, e rodar a cada 15 min consome ~2.900 min/mês. Em repositório
público, Actions é ilimitado. O `estado.json` fica visível no repo, mas só
contém datas.

## Armadilhas conhecidas

- `monitor.yml` fora de `.github/workflows/` é ignorado silenciosamente —
  causa mais comum de "a aba Actions está vazia".
- O cron do GitHub atrasa 5–20 min e às vezes pula execuções. Não é
  confiável para disputa de data popular; nesse caso, rode `--loop 600` em
  paralelo numa máquina própria (os dois podem apontar pro mesmo Telegram,
  cada um com seu `estado.json`).
- Workflows agendados são desativados após 60 dias sem atividade no
  repositório.
- Não baixe o intervalo abaixo de 10 min: é site público de entidade sem
  fins lucrativos, e um bloqueio de IP viria justamente no dia da
  liberação.
