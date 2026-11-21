#!/bin/sh
# text_stats: a contract 1 service in sh (sdk/CONTRACT.md §3) for AMSYOS Node on Linux.
# It counts a text file and, for the isolation check, says what this job can do in each
# folder of `probe`: list it, make a file there; never what is in them.
set -eu
in=$(amsyos-job input text)
[ -n "$in" ] || { amsyos-job fail "text: give a text file (an asset:// reference)"; exit 1; }
top=$(amsyos-job param top)
[ "$top" = "null" ] && top=5
probe=$(amsyos-job param probe --raw)
[ "$probe" = "null" ] && probe=""
out=$(amsyos-job output report .json)
lines=$(grep -c . "$in" || true)
words=$(wc -w < "$in" | tr -d ' ')
chars=$(wc -m < "$in" | tr -d ' ')
most=$(tr -cs '[:alnum:]' '\n' < "$in" | tr '[:upper:]' '[:lower:]' | grep . | sort | uniq -c |
  sort -k1,1nr -k2,2 | head -n "$top" |
  awk 'BEGIN{ORS=""} {if (NR>1) print ", "; printf "{\"word\": \"%s\", \"count\": %d}", $2, $1}')
probe() {
  if ls -A "$1" >/dev/null 2>&1; then r=readable; else r=denied; fi
  f="$1/.amsyos-probe-$$"
  if (: > "$f") 2>/dev/null; then
    rm -f "$f"
    if [ "$r" = readable ]; then r="readable, writable"; else r=writable; fi
  fi
  echo "$r"
}
iso="\"own_folder\": \"$(probe "$AMSYOS_JOB")\""
old_ifs=$IFS; IFS=,
for p in $probe; do
  p=$(echo "$p" | sed 's/^ *//; s/ *$//')
  [ -n "$p" ] || continue
  iso="$iso, \"$p\": \"$(probe "$p")\""
done
IFS=$old_ifs
printf '{"lines": %s, "words": %s, "characters": %s, "most_frequent": [%s], "ran_as": "%s", "machine": "%s", "cwd": "%s", "isolation": {%s}}\n' \
  "$lines" "$words" "$chars" "$most" "$(id -un)" "$(hostname)" "$(pwd)" "$iso" > "$out"
