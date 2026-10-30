#!/usr/bin/env bash
# Запускается НА НОУТБУКЕ из папки ~/nkt: копирует бота на сервер Oracle и включает службу.
# Первый запуск ставит всё нужное; следующие запуски просто обновляют файлы и перезапускают бота.
set -euo pipefail
SERVER="${NTIN_SERVER:?Укажите сервер: NTIN_SERVER=user@host ./deploy_bot.sh}"
cd "$(dirname "$0")"

for f in bot.py check_ntin.py oktru.py nkt_client.py ai_add.py ai_flow.py card.py dicts.py export_drafts.py submit.py submitter.py base_ntin.csv .env server_setup.sh; do
  [ -f "$f" ] || { echo "Нет файла $f в $(pwd)"; exit 1; }
done

echo "== Проверяю связь с сервером"
ssh "$SERVER" 'echo "Связь есть: $(hostname), $(uname -m)"; free -m | sed -n 2p'
echo "== Копирую файлы"
ssh "$SERVER" 'mkdir -p ~/nkt'
scp -q bot.py check_ntin.py oktru.py nkt_client.py ai_add.py ai_flow.py card.py dicts.py export_drafts.py submit.py submitter.py base_ntin.csv .env server_setup.sh "$SERVER":~/nkt/
echo "== Настраиваю и запускаю на сервере"
ssh -t "$SERVER" 'bash ~/nkt/server_setup.sh'
