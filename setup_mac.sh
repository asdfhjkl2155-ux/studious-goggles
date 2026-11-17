#!/bin/bash
# Sets up the video bot on a Mac: checks the machine, installs everything into ./venv,
# downloads the local LTX-2 model and runs a short test generation.
#
#   bash setup_mac.sh
#
# Safe to run again: finished steps are skipped.
set -euo pipefail
cd "$(dirname "$0")"

MODEL_REPO="${LOCAL_MODEL_REPO:-prince-canuma/LTX-2-distilled}"
MIN_RAM_GB=16
GOOD_RAM_GB=32
MIN_DISK_GB=60   # estimate: model weights + text encoder + working space

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\n[X] %s\n' "$*" >&2; exit 1; }

say "1/5 فحص الجهاز"
[ "$(uname -s)" = "Darwin" ] || fail "هذا السكربت للماك فقط."
[ "$(uname -m)" = "arm64" ] || fail "الجهاز بمعالج Intel. الموديل المحلي يحتاج شريحة Apple (M1 أو أحدث). استخدم fal بدلاً منه."
CHIP="$(sysctl -n machdep.cpu.brand_string)"
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
DISK_GB=$(df -g . | awk 'NR==2 {print $4}')
echo "الشريحة: $CHIP"
echo "الذاكرة: ${RAM_GB}GB"
echo "المساحة الفارغة: ${DISK_GB}GB"
[ "$RAM_GB" -ge "$MIN_RAM_GB" ] || fail "الذاكرة ${RAM_GB}GB أقل من الحد الأدنى ${MIN_RAM_GB}GB. التوليد المحلي غير عملي على هذا الجهاز، استخدم fal."
[ "$DISK_GB" -ge "$MIN_DISK_GB" ] || fail "المساحة الفارغة ${DISK_GB}GB. فرّغ حتى ${MIN_DISK_GB}GB على الأقل ثم أعد التشغيل."
if [ "$RAM_GB" -lt "$GOOD_RAM_GB" ]; then
  echo "تنبيه: الذاكرة أقل من ${GOOD_RAM_GB}GB. استخدم جودة 720p ومدة قصيرة (3-5 ثوانٍ)، وأغلق البرامج الأخرى أثناء التوليد."
fi

say "2/5 البحث عن Python 3.11 أو أحدث"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
if [ -z "$PYTHON" ]; then
  command -v brew >/dev/null 2>&1 || fail "Python 3.11+ غير موجود. ثبّته من https://www.python.org/downloads/ ثم أعد التشغيل."
  brew install python@3.12
  PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
fi
echo "سيُستخدم: $("$PYTHON" --version)"
command -v git >/dev/null 2>&1 || fail "git غير موجود. نفّذ: xcode-select --install ثم أعد التشغيل."

say "3/5 تثبيت المكتبات داخل ./venv"
[ -x venv/bin/python ] || "$PYTHON" -m venv venv
venv/bin/python -m pip install --quiet --upgrade pip
venv/bin/python -m pip install --quiet -r requirements.txt
venv/bin/python -m pip install --quiet "git+https://github.com/Blaizzy/mlx-video.git"

say "4/5 تنزيل الموديل وتجربة توليد قصيرة (أول مرة: تنزيل عشرات الجيجابايت، قد يأخذ وقتاً طويلاً)"
TEST_OUT="$(mktemp -d)/test.mp4"
venv/bin/python -m mlx_video.models.ltx_2.generate \
  --prompt "A red fox walking through snow at sunrise, cinematic" \
  --width 512 --height 320 --num-frames 25 --fps 24 \
  --model-repo "$MODEL_REPO" --output-path "$TEST_OUT"
[ -s "$TEST_OUT" ] || fail "انتهت التجربة بدون ملف فيديو. انسخ الرسائل أعلاه وأرسلها للمساعدة."
cp "$TEST_OUT" ./test_video.mp4
echo "نجحت التجربة: test_video.mp4"

say "5/5 إعداد ملف .env"
if [ ! -f .env ]; then
  cp .env.example .env
  # No fal key yet, so make the local model the default.
  sed -i '' 's/^DEFAULT_MODEL=.*/DEFAULT_MODEL=local/' .env
  echo "تم إنشاء .env والموديل الافتراضي هو المحلي."
else
  echo ".env موجود، لم يتم تغييره. للموديل المحلي كافتراضي ضع: DEFAULT_MODEL=local"
fi

cat <<'DONE'

تم التثبيت. الخطوات الباقية:
  1. افتح ملف .env وضع TELEGRAM_BOT_TOKEN (من @BotFather).
  2. شغّل البوت:   venv/bin/python bot.py
  3. أرسل /id للبوت، ضع الرقم في ALLOWED_USER_IDS داخل .env، ثم أعد تشغيل البوت.
  4. اكتب وصف الفيديو في رسالة.
DONE
