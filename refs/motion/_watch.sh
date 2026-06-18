#!/bin/zsh
DEST_DIR="/Users/jonathanmorris/firstback/refs/motion"
LOG="$DEST_DIR/_watcher.log"
SEEN="$DEST_DIR/.seen_list"
CUR="$DEST_DIR/.cur_list"
TMPROOT="/var/folders/47/x2xfm07j5yx_17zz51tf2rmw0000gn/T"
SCAN() { find "$TMPROOT" "$HOME/Desktop" "$HOME/Downloads" -iname '*.mov' 2>/dev/null | sort; }
SCAN > "$SEEN"
: > "$LOG"
echo "watcher started $(date +%T); baseline $(wc -l < "$SEEN" | tr -d ' ') existing .mov" >> "$LOG"
end=$((SECONDS+1800))   # run 30 min
while [ "$SECONDS" -lt "$end" ]; do
  SCAN > "$CUR"
  new=$(comm -23 "$CUR" "$SEEN")
  if [ -n "$new" ]; then
    printf '%s\n' "$new" | while IFS= read -r f; do
      [ -z "$f" ] && continue
      out="$DEST_DIR/caught_$(date +%H%M%S)_${RANDOM}.mov"
      if cp "$f" "$out" 2>/dev/null && [ -s "$out" ]; then
        echo "CAUGHT $(date +%T) <- $f  ->  $(basename "$out")  ($(stat -f%z "$out" 2>/dev/null) bytes)" >> "$LOG"
      fi
    done
    cp "$CUR" "$SEEN"
  fi
  sleep 1
done
echo "watcher exited $(date +%T)" >> "$LOG"
