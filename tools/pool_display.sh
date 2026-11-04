#!/bin/bash
# Make sure a scratch pool display exists at its size, without recreating it (quest-wall-screen AGENTS.md: scratch
# virtual displays come from a fixed pool and are never removed; POST /displays always recreates, and every
# create/remove raises the laptop's colorsync load for good). Runs ON the laptop:
#   ssh pinpin-air "bash -s 47 1920 1200 1" < tools/pool_display.sh
ID=$1 W=$2 H=$3 S=${4:-1}
if ! curl -s -m10 127.0.0.1:57710/displays | /usr/bin/python3 -c "import json,sys; sys.exit(0 if any(d['screen']==$ID and d['w']==$W*$S and d['h']==$H*$S for d in json.load(sys.stdin)) else 1)"; then
  curl -s -m40 -X POST "127.0.0.1:57710/displays?id=$ID&w=$W&h=$H&scale=$S" >/dev/null; sleep 3
  echo "pool display $ID created"
fi
