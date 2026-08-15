#!/bin/bash

set -euo pipefail

autodesk_paths=(
  '/Users/annagorelova/Applications/Remove Autodesk Fusion.app'
  '/Users/annagorelova/Applications/Autodesk Fusion Service Utility.app'
  '/Users/annagorelova/Applications/Autodesk Fusion.app'
  '/Users/annagorelova/Library/Application Support/Autodesk'
  '/Users/annagorelova/Library/WebKit/com.autodesk.AdskIdentityManager'
  '/Users/annagorelova/Library/Preferences/com.autodesk.AdskIdentityManager.plist'
  '/Users/annagorelova/Library/Preferences/com.autodesk.EAGLE 9.7.0.plist'
  '/Users/annagorelova/Library/Preferences/com.autodesk.FusionApp.plist'
  '/Users/annagorelova/Library/Preferences/com.autodesk.fusion360.plist'
  '/Users/annagorelova/Library/HTTPStorages/com.Autodesk.streamer'
  '/Users/annagorelova/Library/Logs/autodesk.webdeploy.streamer.log'
  '/Users/annagorelova/Library/Caches/com.Autodesk.streamer'
  '/Users/annagorelova/Library/Caches/com.autodesk.AdskIdentityManager'
  '/Users/annagorelova/Library/Caches/ADPWebView/Autodesk Fusion'
)

telegram_cache_roots=(
  '/Users/annagorelova/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/stable/account-7208342907352966638/postbox/media'
  '/Users/annagorelova/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/stable/account-3983327447709970463/postbox/media'
  '/Users/annagorelova/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/stable/account-7208342907352966638/cached'
  '/Users/annagorelova/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/stable/account-3983327447709970463/cached'
  '/Users/annagorelova/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/stable/trlottie-animations'
  '/Users/annagorelova/Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/stable/temp'
)

application_cache_roots=(
  '/Users/annagorelova/Library/Caches/Google'
  '/Users/annagorelova/Library/Caches/com.GGG.PathOfExile'
)

remove_tree() {
  local cleanup_tree="$1"
  # Fusion marks local design-cache files as user-immutable (uchg).
  # Clear only that flag within the already-validated cleanup target.
  /usr/bin/chflags -R nouchg "$cleanup_tree"
  /bin/rm -rf -- "$cleanup_tree"
}

clear_directory() {
  local cleanup_directory="$1"
  /usr/bin/chflags -R nouchg "$cleanup_directory"
  /usr/bin/find "$cleanup_directory" -mindepth 1 -delete
}

main() {
  # Avoid Telegram recreating or locking cache entries during cleanup.
  osascript -e 'tell application "Telegram" to quit' 2>/dev/null || true

  for cleanup_autodesk_path in "${autodesk_paths[@]}"; do
    if [[ -e "$cleanup_autodesk_path" || -L "$cleanup_autodesk_path" ]]; then
      remove_tree "$cleanup_autodesk_path"
    fi
  done

  # Clear cache contents while retaining the expected directory structure.
  # Account databases, metadata, preferences, notification keys, and Keychain
  # credentials are intentionally not included in this list.
  for cleanup_telegram_root in "${telegram_cache_roots[@]}"; do
    if [[ -d "$cleanup_telegram_root" ]]; then
      clear_directory "$cleanup_telegram_root"
    fi
  done

  # These are disposable application caches only. Google browser profiles,
  # cookies, passwords, credentials, and Path of Exile settings live elsewhere.
  for cleanup_application_cache in "${application_cache_roots[@]}"; do
    if [[ -d "$cleanup_application_cache" ]]; then
      clear_directory "$cleanup_application_cache"
    fi
  done

  echo 'Cleanup complete. Current disk space:'
  /bin/df -h /
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
