#!/usr/bin/env bash
# Read-only native release gate for the actual post-staple distribution image.
set -euo pipefail

if [[ $# != 4 || "$(uname -s)" != Darwin ]]; then
  echo 'Usage (macOS only): verify_macos_dmg.sh <dmg> <arm64|x86_64> <version> <team-id>' >&2
  exit 1
fi
dmg="$1"
arch="$2"
version="$3"
team="$4"
if [[ ! -s "$dmg" || ! "$arch" =~ ^(arm64|x86_64)$ || -z "$version" || ! "$team" =~ ^[A-Z0-9]{10}$ ]]; then
  echo 'Invalid or missing macOS release verification input.' >&2
  exit 1
fi

# An ad-hoc signature or a certificate from a different team is not release trust.
requirement="anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"$team\""
hdiutil verify "$dmg"
codesign --verify --strict --verbose=2 -R "$requirement" "$dmg"
xcrun stapler validate "$dmg"
if [[ "$(LC_ALL=C spctl --status)" != 'assessments enabled' ]]; then
  echo 'Gatekeeper assessments must be enabled on the verification runner.' >&2
  exit 1
fi
spctl --assess --type open --context context:primary-signature --ignore-cache --no-cache --verbose=2 "$dmg"

mount="$(mktemp -d "${TMPDIR:-/tmp}/tofu-dmg.XXXXXXXX")"
attached=false
cleanup() {
  if $attached; then hdiutil detach "$mount" || return; fi
  rmdir "$mount"
}
trap cleanup EXIT
hdiutil attach -readonly -nobrowse -noautoopen -mountpoint "$mount" "$dmg"
attached=true
app="$mount/Tofu.app"
plist="$app/Contents/Info.plist"
if [[ ! -d "$app" || -L "$app" || "$(readlink "$mount/Applications")" != '/Applications' ||
      "$(plutil -extract CFBundleIdentifier raw -o - "$plist")" != com.tofu.desktop ||
      "$(plutil -extract CFBundleShortVersionString raw -o - "$plist")" != "$version" ||
      "$(plutil -extract CFBundleExecutable raw -o - "$plist")" != Tofu ]]; then
  echo 'Unexpected Tofu disk image layout, app identity or version.' >&2
  exit 1
fi
for executable in "$app/Contents/MacOS/Tofu" "$app/Contents/Frameworks/tofu-db"; do
  if [[ ! -x "$executable" || "$(lipo -archs "$executable")" != "$arch" ]]; then
    echo "Missing executable or wrong architecture (expected $arch): $executable" >&2
    exit 1
  fi
done
codesign --verify --deep --strict --all-architectures --verbose=2 -R "$requirement" "$app"
spctl --assess --type execute --ignore-cache --no-cache --verbose=2 "$app"
# Fail the gate on detach failure as well; never publish a success receipt from a partial run.
cleanup
trap - EXIT
