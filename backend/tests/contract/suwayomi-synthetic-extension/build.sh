#!/usr/bin/env bash
# Build the synthetic extension APK: ./build.sh  ->  build/synthetic.apk
# Needs a JDK 17+, the Android SDK (ANDROID_HOME) and `gradle`; all are on GitHub's ubuntu runners.
# The dependencies are fetched here, before the bench exists; nothing else touches the network.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd "$here/extension"
gradle --no-daemon --console=plain assembleRelease
mkdir -p "$here/build"
apk=$(find build/outputs/apk -name '*.apk' | head -n1)
test -n "$apk" || { echo "no apk produced" >&2; exit 1; }
cp "$apk" "$here/build/synthetic.apk"
echo "built $here/build/synthetic.apk"
