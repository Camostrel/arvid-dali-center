#!/bin/bash
# publish_public.sh — выложить текущее состояние в ПУБЛИЧНЫЙ репозиторий (для HACS).
#
# ЗАЧЕМ. Репозиториев два, и они расходятся молча:
#   • приватный  Camostrel/arvid-ha-dali-center — вся работа: код, docs (спека вендора),
#     выгрузки объектов, карты имён, паркеты;
#   • публичный  Camostrel/arvid-dali-center     — только дистрибутив для HACS.
# Клиент ставит интеграцию из ПУБЛИЧНОГО. Если забыть выложить — у него останется старая
# версия, а мы будем считать, что фикс доехал. Поэтому выкладка делается скриптом, а не руками.
#
# ⛔ ЧТО НИКОГДА НЕ УЕЗЖАЕТ (проверяется ниже): docs/ (там спека вендора PDF), выгрузки и
# карты объектов (tools/voronezh, tools/office_test), сгенерированные apply_*.py, конфиги
# с токенами (*.conf), CLAUDE.md, parquet/.
#
# ЗАПУСК (из корня приватного репозитория):
#   bash tools/publish_public.sh            # собрать и показать, что изменится (без пуша)
#   bash tools/publish_public.sh --push     # выложить и поставить тег версии

set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST="${PUBLIC_REPO:-$HOME/nicksha/arvid-dali-center-public}"
PUSH="${1:-}"

[ -d "$DST/.git" ] || { echo "нет публичного репозитория в $DST (задайте PUBLIC_REPO=путь)"; exit 2; }

VERSION=$(python3 -c "import json;print(json.load(open('$SRC/custom_components/arvid_dali_center/manifest.json'))['version'])")
echo "версия из manifest: $VERSION"

# 1) ГЕЙТ: приватное дерево должно быть чистым — иначе выложим полуфабрикат
if [ -n "$(git -C "$SRC" status --porcelain)" ]; then
    echo "⚠ приватный репозиторий НЕ ЗАКОММИЧЕН — сначала коммит, потом выкладка"
    git -C "$SRC" status --short | head -10
    exit 1
fi

# 2) ГЕЙТ: тесты. Выкладывать красное нельзя — клиент ставит именно это
( cd "$SRC" && python3 -m unittest discover -s tests >/dev/null 2>&1 ) \
    || { echo "⚠ ТЕСТЫ ПАДАЮТ — выкладка отменена"; exit 1; }
echo "тесты зелёные"

# 3) синхронизация состава (полная замена каталогов: удалённые файлы должны исчезать и там)
rm -rf "$DST/custom_components" "$DST/www" "$DST/blueprints" "$DST/tests" "$DST/tools"
mkdir -p "$DST/custom_components" "$DST/www" "$DST/tests" "$DST/tools"
cp -r "$SRC/custom_components/arvid_dali_center" "$DST/custom_components/"
cp "$SRC"/www/*.js "$DST/www/"
[ -d "$SRC/blueprints" ] && cp -r "$SRC/blueprints" "$DST/"
cp "$SRC"/tests/*.py "$DST/tests/"
cp "$SRC"/tools/*.py "$SRC"/tools/*.yaml "$SRC"/tools/*.md "$SRC"/tools/*.sh "$DST/tools/" 2>/dev/null || true
cp "$SRC/hacs.json" "$DST/"
cp "$SRC/tools/README_PUBLIC.md" "$DST/README.md"       # README публичного живёт в приватном
rm -f "$DST/tools/README_PUBLIC.md"                      # в публичном он не нужен вторым файлом

# 4) вычистить то, что не должно уезжать (двойная защита к списку копирования)
rm -rf "$DST/tools/voronezh" "$DST/tools/office_test"
rm -f "$DST"/tools/apply_*.py "$DST"/tools/*.conf
find "$DST" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$DST" -name "*.pyc" -delete 2>/dev/null || true

# 5) КОНТРОЛЬ: ничего чувствительного не просочилось
BAD=$(cd "$DST" && find . -path ./.git -prune -o \( -name "*.pdf" -o -name "*.parquet" -o -name "*.csv" -o -name "*.conf" \) -print | head)
[ -z "$BAD" ] || { echo "⚠ в дистрибутив попало лишнее:"; echo "$BAD"; exit 1; }
TOKENS=$(cd "$DST" && grep -rlE "ha_token: eyJ[A-Za-z0-9_-]{20,}|BEGIN [A-Z ]*PRIVATE KEY" . 2>/dev/null | head || true)
[ -z "$TOKENS" ] || { echo "⚠ похоже на настоящий токен/ключ:"; echo "$TOKENS"; exit 1; }

# 6) показать/выложить
cd "$DST"
if [ -z "$(git status --porcelain)" ]; then
    echo "публичный репозиторий уже актуален (v$VERSION) — выкладывать нечего"
    exit 0
fi
echo "── изменения для публикации ──"
git status --short | head -20

if [ "$PUSH" != "--push" ]; then
    echo
    echo "это ПРЕДПРОСМОТР. Выложить: bash tools/publish_public.sh --push"
    exit 0
fi

git add -A
git -c user.email=noreply@anthropic.com -c user.name="ARVID" commit -q -m "v$VERSION"
git tag -f "v$VERSION" >/dev/null
git push -q origin main
git push -qf origin "v$VERSION"
echo "✅ выложено: v$VERSION → github.com/Camostrel/arvid-dali-center (тег поставлен)"
