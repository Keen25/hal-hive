#!/usr/bin/env bash
# HAL master-orchestrator startup banner.
# Usage: bash ~/hive/hal-banner.sh   (or: source it from ~/.bashrc)
# Honours NO_COLOR / non-tty: falls back to plain ASCII, no escape codes.

if [ -n "$NO_COLOR" ] || [ ! -t 1 ]; then
  R= ; W= ; G= ; B= ; D= ; X=
else
  R=$'\033[31m'   ; W=$'\033[1;97m' ; G=$'\033[90m'
  B=$'\033[91m'   ; D=$'\033[1;91m' ; X=$'\033[0m'
fi

printf '\n'
printf '  %s██╗  ██╗ █████╗ ██╗%s        %s▄▄▄▄▄%s\n'          "$W" "$X" "$R" "$X"
printf '  %s██║  ██║██╔══██╗██║%s      %s▄█%s█████%s█▄%s\n'     "$W" "$X" "$R" "$B" "$R" "$X"
printf '  %s███████║███████║██║%s     %s██%s██%s●%s██%s██%s     %sHAL 9000%s\n' \
        "$W" "$X" "$R" "$B" "$D" "$B" "$R" "$X" "$G" "$X"
printf '  %s██╔══██║██╔══██║██║%s     %s██%s███████%s██%s\n'    "$W" "$X" "$R" "$B" "$R" "$X"
printf '  %s██║  ██║██║  ██║███████╗%s %s▀█%s█████%s█▀%s\n'     "$W" "$X" "$R" "$B" "$R" "$X"
printf '  %s╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝%s  %s▀▀▀▀▀%s\n'            "$W" "$X" "$R" "$X"
printf '  %smaster orchestrator%s %s·%s %sARS lab%s %s·%s %s●%s %sred eye online%s\n' \
        "$G" "$X" "$D" "$X" "$G" "$X" "$D" "$X" "$R" "$X" "$G" "$X"
printf '\n'
