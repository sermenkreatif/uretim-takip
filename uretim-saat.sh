#!/bin/bash
# Telefondan gelen saatle Pi saatini düzeltir (yalnızca sayı kabul eder).
[[ "${1:-}" =~ ^[0-9]{9,11}$ ]] || exit 1
date -s "@$1" >/dev/null && (hwclock -w 2>/dev/null || true) && (fake-hwclock save 2>/dev/null || true)
