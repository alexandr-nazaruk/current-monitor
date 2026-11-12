#!/bin/bash

# Одноразовий деплой: копіює файли на Raspberry Pi і перезапускає сервіс cmon.


SRC_DIR="src"
LOCAL_FILES=("$SRC_DIR/cmon.py" "$SRC_DIR/cmon_secrets.py")
REMOTE_HOST="nsh@192.168.10.101"
REMOTE_DEST="$REMOTE_HOST:"
SERVICE="cmon"

set -e

# Працює з будь-якої директорії: відносні шляхи рахуються від папки скрипта
cd "$(dirname "$0")"

SSH_KEY="cmon_rsa"
SSH_OPTS=(-i "$SSH_KEY" -o IdentitiesOnly=yes)

echo "Копіювання ${LOCAL_FILES[*]}..."
rsync -avz -e "ssh ${SSH_OPTS[*]}" "${LOCAL_FILES[@]}" "$REMOTE_DEST"

echo "Перезапуск сервісу $SERVICE..."
ssh "${SSH_OPTS[@]}" -t "$REMOTE_HOST" "sudo systemctl restart $SERVICE && systemctl is-active $SERVICE"

echo "Готово."
