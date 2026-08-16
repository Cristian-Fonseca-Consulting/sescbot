#!/usr/bin/env python3
"""
Monitor de liberacao de datas - SESC PR (Reserva Online)

Semantica dos endpoints:
  DatasIndisponiveis -> datas que AINDA NAO foram liberadas para reserva.
                        Quando uma data SAI dessa lista, abriu a reserva.
  DatasBloqueio      -> datas em que a unidade nao opera / nao vai liberar.

O bot guarda um retrato das duas listas a cada execucao e avisa quando
QUALQUER data (nao so fim de semana) deixa de estar indisponivel. A
mensagem lista todas as datas abertas no momento, do dia de hoje ate a
fronteira do calendario publicado pelo site, com o dia da semana de cada
uma.

Como o GitHub Actions nao mantem processo ligado, os comandos /status e
/fila do bot Telegram sao respondidos por polling: ao final de cada
execucao, o script chama getUpdates com o offset salvo em estado.json e
responde aos comandos acumulados desde a ultima rodada. Latencia de ate
15 min (intervalo do agendamento) e esperada.

Uso:
    python sesc_watch.py                 # roda uma vez
    python sesc_watch.py --loop 1800     # roda a cada 30 min
    python sesc_watch.py --status        # so mostra a situacao, nao notifica
    python sesc_watch.py --reset         # apaga a baseline
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

BASE = "https://www.sescpr.com.br/ReservaOnline/Reserva"
URL_INDISPONIVEIS = f"{BASE}/DatasIndisponiveis"
URL_BLOQUEIO = f"{BASE}/DatasBloqueio"

# ---------------------------------------------------------------- config ----

COD_MEIO_HOSPEDAGEM = os.getenv("SESC_COD_MEIO_HOSPEDAGEM", "34")
NOME_UNIDADE = os.getenv("SESC_NOME_UNIDADE", f"unidade {COD_MEIO_HOSPEDAGEM}")

STATE_FILE = Path(os.getenv("SESC_STATE_FILE", "estado.json"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")

TIMEOUT = 30
DIAS_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]

# ------------------------------------------------------------------ http ----


def _sessao():
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://www.sescpr.com.br",
        "Referer": f"{BASE}/Index",
    })
    return s


def buscar(sessao, url, tentativas=3):
    """POST no endpoint com retry, devolve set de datas."""
    for n in range(1, tentativas + 1):
        try:
            r = sessao.post(url, data={"codMeioHospedagem": COD_MEIO_HOSPEDAGEM}, timeout=TIMEOUT)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            if n == tentativas:
                raise
            espera = 5 * n
            print(f"[aviso] tentativa {n}/{tentativas} falhou ({e}); "
                  f"nova tentativa em {espera}s", file=sys.stderr)
            time.sleep(espera)
    dados = r.json()
    if isinstance(dados, dict):
        for chave in ("data", "Data", "datas", "result"):
            if chave in dados:
                dados = dados[chave]
                break
    if not isinstance(dados, list):
        raise ValueError(f"resposta inesperada de {url}: {str(dados)[:200]}")
    return {d for d in (parse_data(x) for x in dados) if d}


# ------------------------------------------------------------------ datas ---


def parse_data(txt):
    txt = str(txt).strip()
    for fmt_ in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(txt[:10], fmt_).date()
        except ValueError:
            continue
    return None


def fmt(d):
    return f"{DIAS_PT[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def eh_fds(d):
    """Sexta, sabado ou domingo."""
    return d.weekday() in (4, 5, 6)


def horizonte(indisponiveis, bloqueios):
    """Ultima data que o site publica. Alem disso nao sabemos nada."""
    conhecidas = indisponiveis | bloqueios
    return max(conhecidas) if conhecidas else date.today()


def datas_abertas(indisponiveis, bloqueios, inicio=None, limite=None):
    """Todas as datas livres para reserva agora, de qualquer dia da semana,
    entre inicio e a fronteira do calendario publicado."""
    inicio = inicio or date.today()
    limite = limite or horizonte(indisponiveis, bloqueios)
    ocupadas = indisponiveis | bloqueios
    abertas = []
    d = inicio
    while d <= limite:
        if d not in ocupadas:
            abertas.append(d)
        d += timedelta(days=1)
    return abertas


def fila_de_espera(indisponiveis, bloqueios):
    """Dias de FDS aguardando liberacao, agrupados por mes."""
    pendentes = sorted(d for d in indisponiveis if eh_fds(d) and d not in bloqueios and d >= date.today())
    por_mes = {}
    for d in pendentes:
        por_mes.setdefault(d.strftime("%m/%Y"), []).append(d)
    return por_mes


# ----------------------------------------------------------------- estado ---


def ler_estado():
    if not STATE_FILE.exists():
        return None
    try:
        e = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "indisponiveis": {date.fromisoformat(x) for x in e.get("indisponiveis", [])},
            "bloqueios": {date.fromisoformat(x) for x in e.get("bloqueios", [])},
            "abertas": {date.fromisoformat(x) for x in e.get("abertas", [])},
            "ultima_execucao": e.get("ultima_execucao"),
            "telegram_offset": e.get("telegram_offset", 0),
        }
    except (json.JSONDecodeError, ValueError, OSError) as err:
        print(f"[aviso] estado ilegivel ({err}), recriando baseline", file=sys.stderr)
        return None


def salvar_estado(indisponiveis, bloqueios, abertas, telegram_offset=0):
    STATE_FILE.write_text(json.dumps({
        "indisponiveis": sorted(d.isoformat() for d in indisponiveis),
        "bloqueios": sorted(d.isoformat() for d in bloqueios),
        "abertas": sorted(d.isoformat() for d in abertas),
        "ultima_execucao": datetime.now().isoformat(timespec="seconds"),
        "telegram_offset": telegram_offset,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------ notificacao ---


def notificar(titulo, corpo):
    enviado = False
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": f"*{titulo}*\n\n{corpo}",
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=TIMEOUT).raise_for_status()
            enviado = True
        except requests.RequestException as e:
            print(f"[erro] telegram: {e}", file=sys.stderr)

    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}", data=corpo.encode("utf-8"),
                headers={"Title": titulo.encode("utf-8"), "Priority": "high", "Tags": "hotel"},
                timeout=TIMEOUT).raise_for_status()
            enviado = True
        except requests.RequestException as e:
            print(f"[erro] ntfy: {e}", file=sys.stderr)

    if not enviado:
        print(f"\n*** {titulo} ***\n{corpo}\n")


# ------------------------------------------------------------- bot: comandos

def enviar_mensagem(chat_id, texto):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": texto,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=TIMEOUT).raise_for_status()
    except requests.RequestException as e:
        print(f"[erro] telegram sendMessage: {e}", file=sys.stderr)


def obter_atualizacoes(offset):
    """Poll manual via getUpdates (sem long polling: timeout=0)."""
    if not TELEGRAM_TOKEN:
        return [], offset
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=TIMEOUT)
        r.raise_for_status()
        dados = r.json()
    except requests.RequestException as e:
        print(f"[erro] telegram getUpdates: {e}", file=sys.stderr)
        return [], offset

    if not dados.get("ok"):
        return [], offset

    resultados = dados.get("result", [])
    novo_offset = offset
    for u in resultados:
        novo_offset = max(novo_offset, u["update_id"] + 1)
    return resultados, novo_offset


def fmt_ts(iso):
    """Formata o timestamp ISO de ultima_execucao como dd/mm/yyyy HH:MM."""
    if not iso:
        return "agora"
    return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M")


def texto_status(abertas, bloqueios, limite, ultima_execucao):
    fds_abertas = sum(1 for d in abertas if eh_fds(d))
    linhas = [
        f"*Status - {NOME_UNIDADE}*",
        "",
        f"Ultima verificacao: {fmt_ts(ultima_execucao)}",
        f"Calendario publicado ate: {fmt(limite)}",
        "",
        f"Datas abertas agora: {len(abertas)} ({fds_abertas} de fim de semana)",
        f"Datas bloqueadas: {len(bloqueios)}",
    ]
    if abertas:
        linhas.append("")
        linhas.append("*Abertas agora:*")
        linhas += [f"  - {fmt(d)}" for d in abertas]
    return "\n".join(linhas)


def texto_fila(espera):
    if not espera:
        return "Fila vazia: nenhum dia de FDS aguardando liberacao dentro do calendario publicado."
    linhas = ["*Fila de espera (FDS ainda indisponiveis)*", ""]
    for mes, dias in espera.items():
        dias_str = ", ".join(str(d.day) for d in dias)
        linhas.append(f"*{mes}:* {dias_str}")
    return "\n".join(linhas)


def processar_comandos(offset, abertas, bloqueios, indisponiveis, limite, ultima_execucao):
    """Le comandos acumulados desde a ultima rodada e responde. Devolve o novo offset."""
    atualizacoes, novo_offset = obter_atualizacoes(offset)
    if not atualizacoes:
        return novo_offset

    espera = fila_de_espera(indisponiveis, bloqueios)
    for u in atualizacoes:
        # canal manda "channel_post", chat privado/grupo manda "message"
        msg = u.get("message") or u.get("channel_post") or {}
        texto = (msg.get("text") or "").strip().lower()
        chat_id = msg.get("chat", {}).get("id")
        if not chat_id or not texto.startswith("/"):
            continue
        if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
            continue

        comando = texto.split()[0].split("@")[0]
        if comando == "/status":
            enviar_mensagem(chat_id, texto_status(abertas, bloqueios, limite, ultima_execucao))
        elif comando == "/fila":
            enviar_mensagem(chat_id, texto_fila(espera))

    return novo_offset


# ------------------------------------------------------------------- main ---


def executar(apenas_status=False):
    sessao = _sessao()
    try:
        indisponiveis = buscar(sessao, URL_INDISPONIVEIS)
        bloqueios = buscar(sessao, URL_BLOQUEIO)
    except (requests.RequestException, ValueError) as e:
        print(f"[erro] falha ao consultar: {e}", file=sys.stderr)
        return 1

    limite = horizonte(indisponiveis, bloqueios)
    abertas = datas_abertas(indisponiveis, bloqueios, limite=limite)
    abertas_set = set(abertas)
    espera = fila_de_espera(indisponiveis, bloqueios)

    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    print(f"[{agora}] {NOME_UNIDADE} | {len(indisponiveis)} nao liberadas, "
          f"{len(bloqueios)} bloqueadas, {len(abertas)} abertas | "
          f"calendario ate {limite.strftime('%d/%m/%Y')}")

    anterior = ler_estado()
    offset = anterior["telegram_offset"] if anterior else 0

    if anterior is None:
        print("  primeira execucao - salvando baseline, sem notificar")
        for d in abertas:
            print(f"    ja aberta: {fmt(d)}")
        for mes, dias in espera.items():
            print(f"    aguardando {mes}: {len(dias)} dias de FDS")
        if not apenas_status:
            offset = processar_comandos(offset, abertas, bloqueios, indisponiveis, limite, None)
            salvar_estado(indisponiveis, bloqueios, abertas_set, offset)
        return 0

    # --- o evento que importa: uma data que saiu da lista de indisponiveis
    novas = sorted(abertas_set - anterior["abertas"])
    # regressao: algo que estava aberto e sumiu (alguem reservou ou re-bloqueou)
    perdidas = sorted(anterior["abertas"] - abertas_set)

    for d in abertas:
        marca = "NOVO" if d in novas else "    "
        print(f"  {marca} {fmt(d)}")
    for mes, dias in espera.items():
        print(f"       aguardando {mes}: {len(dias)} dias de FDS")

    if novas and not apenas_status:
        linhas = [
            "*Novas datas liberadas:*",
            *[f"  - {fmt(d)}" for d in novas],
            "",
            f"*Todas as datas abertas ({len(abertas)}):*",
            *[f"  - {fmt(d)}" for d in abertas],
            "",
            f"Reservar: {BASE}/Index",
        ]
        notificar(f"Datas liberadas - {NOME_UNIDADE}", "\n".join(linhas))

    if perdidas:
        print(f"  [info] {len(perdidas)} data(s) deixaram de estar livres")

    if not apenas_status:
        offset = processar_comandos(offset, abertas, bloqueios, indisponiveis, limite,
                                     anterior["ultima_execucao"])
        salvar_estado(indisponiveis, bloqueios, abertas_set, offset)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Monitor de liberacao de datas - SESC PR")
    ap.add_argument("--loop", type=int, metavar="SEGUNDOS", help="roda continuamente")
    ap.add_argument("--status", action="store_true", help="so mostra, nao notifica nem grava")
    ap.add_argument("--reset", action="store_true", help="apaga a baseline e sai")
    args = ap.parse_args()

    if args.reset:
        STATE_FILE.unlink(missing_ok=True)
        print(f"{STATE_FILE} removido.")
        return

    if not args.loop:
        sys.exit(executar(args.status))

    print(f"Monitorando a cada {args.loop}s. Ctrl+C para parar.")
    while True:
        try:
            executar(args.status)
        except KeyboardInterrupt:
            print("\nEncerrado.")
            break
        except Exception as e:  # noqa: BLE001
            print(f"[erro inesperado] {e}", file=sys.stderr)
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
