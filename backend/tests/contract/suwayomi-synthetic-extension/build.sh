#!/usr/bin/env bash
# Build the synthetic extension APK: ./build.sh  ->  build/synthetic.apk
# Needs a JDK 17 (Temurin), the Android SDK (ANDROID_HOME); Gradle comes from the checked wrapper.
# The dependencies are fetched here, before the bench exists; nothing else touches the network.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd "$here/extension"
./gradlew --no-daemon --console=plain assembleRelease
mkdir -p "$here/build"
apk=$(find build/outputs/apk -name '*.apk' | head -n1)
test -n "$apk" || { echo "no apk produced" >&2; exit 1; }
cp "$apk" "$here/build/synthetic.apk"
echo "built $here/build/synthetic.apk"
