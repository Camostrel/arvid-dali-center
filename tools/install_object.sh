#!/bin/bash
# install_object.sh — разложить КОМПЛЕКТ ОБЪЕКТА по /config. Запускается НА БОКСЕ, в терминале HA.
#
# Кладётся внутрь архива скриптом `make_object_kit.sh` и вызывается оттуда:
#     cd /config && tar xzf arvid_kit_*.tar.gz && bash arvid_kit_*/install.sh
#
# ━━ ЧТО ДЕЛАЕТ ━━
#   код (custom_components, www, blueprints) → перезаписывает: это дистрибутив, его версия одна
#   данные объекта (карта, план, кривые)     → кладёт, ЕСЛИ таких файлов ещё нет
#   `arvid_apply.conf` (токен HA)            → НЕ ТРОГАЕТ НИКОГДА
#
# ━━ ПОЧЕМУ ДАННЫЕ НЕ ПЕРЕЗАПИСЫВАЕТ ━━
# На объекте их правит ЧЕЛОВЕК: карту дополняет по факту, кривые — по замерам ваттметром,
# в конфиг вписывает токен. Перезапись «свежей версией из архива» стёрла бы работу, сделанную
# на месте, и заметили бы это не сразу. Существующий файл сохраняется, новый кладётся рядом с
# суффиксом `.new` — человек сам решает, что с ним делать.

set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
CFG="${CONFIG_DIR:-/config}"

[ -d "$CFG" ] || { echo "нет каталога $CFG — это точно бокс Home Assistant?"; exit 2; }
[ -d "$SRC/code/custom_components" ] || { echo "архив неполный: нет code/custom_components"; exit 2; }

echo "── установка комплекта в $CFG"

# ── 1. КОД (перезаписываем) ───────────────────────────────────────────────────
mkdir -p "$CFG/custom_components" "$CFG/www"
cp -r "$SRC/code/custom_components/." "$CFG/custom_components/"
cp -r "$SRC/code/www/." "$CFG/www/"
if [ -d "$SRC/code/blueprints" ]; then
    mkdir -p "$CFG/blueprints"
    cp -r "$SRC/code/blueprints/." "$CFG/blueprints/"
fi
VER=$(grep -oE 'VERSION = "[^"]+"' "$CFG/custom_components/arvid_dali_center/const.py" | cut -d'"' -f2)
echo "   код установлен, версия $VER"

# ── 2. ДАННЫЕ ОБЪЕКТА (не затираем существующее) ──────────────────────────────
put() {                       # put <файл-источник> <каталог-назначение>
    local src="$1" dir="$2" name
    name="$(basename "$src")"
    mkdir -p "$dir"
    if [ -f "$dir/$name" ]; then
        if cmp -s "$src" "$dir/$name"; then
            echo "   = $dir/$name (уже такой же)"
        else
            cp "$src" "$dir/$name.new"
            echo "   ⚠ $dir/$name СУЩЕСТВУЕТ и отличается → положил рядом $name.new (решать вам)"
        fi
    else
        cp "$src" "$dir/$name"
        echo "   + $dir/$name"
    fi
}

for f in "$SRC/object/namemap/"*.csv;  do [ -e "$f" ] && put "$f" "$CFG/arvid_namemap"; done
for f in "$SRC/object/tools/"*;        do [ -e "$f" ] && put "$f" "$CFG/tools"; done
for f in "$SRC/object/curves/"*.yaml;  do [ -e "$f" ] && put "$f" "$CFG/arvid_curves"; done

# кривые: рабочий файл заводим из образца, если его ещё нет (человек заполнит замерами)
if [ ! -f "$CFG/arvid_curves/curves.yaml" ] && [ -f "$CFG/arvid_curves/curves.example.yaml" ]; then
    cp "$CFG/arvid_curves/curves.example.yaml" "$CFG/arvid_curves/curves.yaml"
    echo "   + $CFG/arvid_curves/curves.yaml (из образца — заполните замерами)"
fi

# заготовка конфига с токеном: создаём ТОЛЬКО если файла нет. Существующий не трогаем никогда —
# в нём токен долгого действия, и перезапись означала бы «прогон молча перестал работать».
if [ ! -f "$CFG/tools/arvid_apply.conf" ]; then
    printf 'ha_token: \n# ha_url: ws://homeassistant:8123/api/websocket\n' > "$CFG/tools/arvid_apply.conf"
    echo "   + $CFG/tools/arvid_apply.conf (впишите токен долгого действия HA)"
else
    echo "   = $CFG/tools/arvid_apply.conf (не трогаю — там токен)"
fi

cat <<EOF

✅ Установлено. Дальше:

  1. Перезапустить Home Assistant (Настройки → Система → Перезапустить).
  2. Ресурсы Lovelace (если ставите впервые): /local/arvid-dali-panel.js и
     /local/arvid-dali-commissioning.js — как модуль.
  3. shell_command в configuration.yaml (нужен для прогона плана):
       shell_command:
         arvid_import_apply: "sh /config/tools/run_apply.sh {{ script }} {{ args }}"
  4. Токен долгого действия — в $CFG/tools/arvid_apply.conf
  5. Порядок пусконаладки — в README_OBJECT.md рядом с этим скриптом.
EOF
