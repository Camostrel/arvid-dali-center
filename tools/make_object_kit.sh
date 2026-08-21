#!/bin/bash
# make_object_kit.sh — собрать КОМПЛЕКТ ОБЪЕКТА одним архивом (запускается ЗДЕСЬ, не на боксе).
#
# ЗАЧЕМ. На объекте нужно поставить интеграцию, карточки и данные конкретного объекта (карта
# имён, план пусконаладки). Через HACS приезжает только `custom_components/` — карта и план
# живут вне пакета, и их пришлось бы носить руками. А тащить на бокс ВЕСЬ репозиторий нельзя:
# там спека вендора, паркеты и данные ДРУГИХ объектов — чужому боксу они не нужны и не должны
# там оказаться.
#
# Поэтому: собираем ровно то, что нужно этому объекту, кладём внутрь установщик и получаем
# один файл. На объекте — закинуть архив (File Editor) и выполнить одну команду в терминале.
#
# ВЫЗОВ:
#   bash tools/make_object_kit.sh voronezh          # комплект для Воронежа
#   bash tools/make_object_kit.sh voronezh /tmp/out # куда положить архив
#
# Данные объекта в ПУБЛИЧНЫЙ репозиторий при этом НЕ уезжают — комплект собирается локально.

set -eu

OBJ="${1:-}"
OUT_DIR="${2:-$(pwd)}"
[ -n "$OBJ" ] || { echo "укажите объект: bash tools/make_object_kit.sh voronezh"; exit 2; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VER=$(grep -oP '(?<=VERSION = ")[^"]+' custom_components/arvid_dali_center/const.py)
STAMP=$(date +%Y%m%d_%H%M)
KIT="arvid_kit_${OBJ}_v${VER}_${STAMP}"
TMP="$(mktemp -d)"
DST="$TMP/$KIT"
mkdir -p "$DST"

echo "── сборка комплекта: объект $OBJ, версия $VER"

# ── 1. КОД: то же, что уезжает в публичный дистрибутив ────────────────────────
mkdir -p "$DST/code"
cp -r custom_components "$DST/code/"
cp -r www "$DST/code/"
[ -d blueprints ] && cp -r blueprints "$DST/code/"
find "$DST/code" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "   код: custom_components + www + blueprints"

# ── 2. ДАННЫЕ ОБЪЕКТА: только этого объекта ───────────────────────────────────
mkdir -p "$DST/object/namemap" "$DST/object/tools" "$DST/object/curves"
case "$OBJ" in
  voronezh)
    cp tools/voronezh/voronezh_name_map.csv "$DST/object/namemap/"
    [ -f tools/voronezh/apply_voronezh.py ] && cp tools/voronezh/apply_voronezh.py "$DST/object/tools/"
    ;;
  *)
    echo "⚠ объект «$OBJ» не описан в скрипте — комплект будет БЕЗ данных объекта"
    ;;
esac
cp tools/run_apply.sh "$DST/object/tools/"
cp tools/curves.example.yaml "$DST/object/curves/"
cp tools/README_OBJECT.md "$DST/"
echo "   данные: карта имён, план, run_apply.sh, образец кривых"

# ── 3. УСТАНОВЩИК (кладётся внутрь; на боксе запускается он) ──────────────────
cp tools/install_object.sh "$DST/install.sh"
chmod +x "$DST/install.sh"

# ── 4. Архив ──────────────────────────────────────────────────────────────────
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/$KIT.tar.gz"
tar -czf "$ARCHIVE" -C "$TMP" "$KIT"
rm -rf "$TMP"

echo
echo "✅ готово: $ARCHIVE  ($(du -h "$ARCHIVE" | cut -f1))"
echo
echo "НА ОБЪЕКТЕ (аддон «Terminal & SSH»):"
echo "  1) закинуть архив в /config (File Editor / Samba) или скачать любым способом"
echo "  2) cd /config && tar xzf $KIT.tar.gz && bash $KIT/install.sh"
echo "  3) перезапустить Home Assistant"
