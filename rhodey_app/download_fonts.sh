#!/bin/bash
# Download Google Fonts for bundling into APK
set -e
DIR="$(cd "$(dirname "$0")" && pwd)/assets/fonts"
cd "$DIR"

echo "Downloading fonts to $DIR..."

# Instrument Serif
curl -sL -o "InstrumentSerif-Italic.ttf" \
  "https://fonts.gstatic.com/s/instrumentserif/v5/jizHRFtNs2ka5fXjeivQ4LroWlx-6zATiw.ttf"
curl -sL -o "InstrumentSerif-Regular.ttf" \
  "https://fonts.gstatic.com/s/instrumentserif/v5/jizBRFtNs2ka5fXjeivQ4LroWlx-2zI.ttf"

# JetBrains Mono
curl -sL -o "JetBrainsMono-Regular.ttf" \
  "https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbY2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKxjPQ.ttf"
curl -sL -o "JetBrainsMono-Medium.ttf" \
  "https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbY2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8-qxjPQ.ttf"

# Plus Jakarta Sans
curl -sL -o "PlusJakartaSans-Light.ttf" \
  "https://fonts.gstatic.com/s/plusjakartasans/v12/LDIbaomQNQcsA88c7O9yZ4KMCoOg4IA6-91aHEjcWuA_907NSg.ttf"
curl -sL -o "PlusJakartaSans-Regular.ttf" \
  "https://fonts.gstatic.com/s/plusjakartasans/v12/LDIbaomQNQcsA88c7O9yZ4KMCoOg4IA6-91aHEjcWuA_qU7NSg.ttf"
curl -sL -o "PlusJakartaSans-Medium.ttf" \
  "https://fonts.gstatic.com/s/plusjakartasans/v12/LDIbaomQNQcsA88c7O9yZ4KMCoOg4IA6-91aHEjcWuA_m07NSg.ttf"

echo "Done. Files:"
ls -lh
