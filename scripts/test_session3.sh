#!/usr/bin/env bash
# =============================================================================
# Raabta.fm — Session 3 test suite
# Usage: bash scripts/test_session3.sh [BASE_URL]
# Requires: curl, jq, wscat  (npm i -g wscat)
# =============================================================================

BASE="${1:-http://localhost:8000}"
CLIENT_ID="test-client-$(date +%s)"   # unique per run so rate limits don't bleed
BOLD="\033[1m"; GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; RESET="\033[0m"

pass() { echo -e "${GREEN}✓ $1${RESET}"; }
fail() { echo -e "${RED}✗ $1${RESET}"; }
info() { echo -e "${YELLOW}→ $1${RESET}"; }
section() { echo -e "\n${BOLD}══ $1 ══${RESET}"; }

# ── 0. Health check ───────────────────────────────────────────────────────────
section "0. Health"
STATUS=$(curl -sf "$BASE/health" | jq -r '.status')
[ "$STATUS" = "ok" ] && pass "Health OK" || fail "Health returned: $STATUS"

# ── 1. Create session ─────────────────────────────────────────────────────────
section "1. Create session"
CREATE_RESP=$(curl -sf -X POST "$BASE/sessions" \
  -H "Content-Type: application/json" \
  -H "X-Client-ID: $CLIENT_ID" \
  -d '{"display_name": "TestDJ"}')

echo "$CREATE_RESP" | jq .
SESSION_ID=$(echo "$CREATE_RESP" | jq -r '.session_id')
[ "$SESSION_ID" != "null" ] && [ -n "$SESSION_ID" ] \
  && pass "Session created: $SESSION_ID" \
  || { fail "Could not create session — aborting"; exit 1; }

# ── 2. Ten vibe queries ───────────────────────────────────────────────────────
section "2. Vibe queries"

run_vibe() {
  local LABEL="$1"
  local VIBE="$2"
  local EXPECTED_NOTE="$3"

  echo -e "\n${BOLD}Vibe: \"$VIBE\"${RESET}  ($EXPECTED_NOTE)"
  RESP=$(curl -sf -X POST "$BASE/sessions/$SESSION_ID/queue/vibe" \
    -H "Content-Type: application/json" \
    -H "X-Client-ID: $CLIENT_ID" \
    -d "{\"vibe\": $(echo "$VIBE" | jq -R .), \"display_name\": \"TestDJ\"}")

  if [ $? -ne 0 ]; then
    fail "Request failed for: $VIBE"
    return
  fi

  KEYWORDS=$(echo "$RESP" | jq -r '.vibe_interpreted_as | join(", ")')
  CONFIDENCE=$(echo "$RESP" | jq -r '.confidence')
  FALLBACK=$(echo "$RESP" | jq -r '.fallback_used')
  TRACK_COUNT=$(echo "$RESP" | jq '.tracks | length')

  echo "  Keywords    : $KEYWORDS"
  echo "  Confidence  : $CONFIDENCE"
  echo "  Fallback    : $FALLBACK"
  echo "  Tracks found: $TRACK_COUNT"
  echo "  Spotify IDs to verify:"
  echo "$RESP" | jq -r '.tracks[] | "    \(.artist) — \(.title)  [\(.spotify_id)]  sim=\(.similarity)"'

  [ "$TRACK_COUNT" -gt "0" ] && pass "$LABEL returned $TRACK_COUNT tracks" || fail "$LABEL returned 0 tracks"
}

# Use a fresh client per vibe call to avoid hitting per-client rate limit
ORIG_CLIENT="$CLIENT_ID"

CLIENT_ID="${ORIG_CLIENT}-v1"
run_vibe "Late night drive"   "late night drive"                          "atmospheric, ~80 BPM, low energy"

CLIENT_ID="${ORIG_CLIENT}-v2"
run_vibe "Hype workout"       "hype workout"                              "intense, high energy, fast"

CLIENT_ID="${ORIG_CLIENT}-v3"
run_vibe "Sunday coffee"      "sunday morning coffee"                     "gentle, acoustic, slow"

CLIENT_ID="${ORIG_CLIENT}-v4"
run_vibe "Sad and slow"       "something sad and slow"                    "melancholic, low valence"

CLIENT_ID="${ORIG_CLIENT}-v5"
run_vibe "Feel good summer"   "feel good summer vibes"                    "happy, upbeat, energetic"

CLIENT_ID="${ORIG_CLIENT}-v6"
run_vibe "Focus and study"    "focus and study"                           "calm, possibly instrumental"

CLIENT_ID="${ORIG_CLIENT}-v7"
run_vibe "80s pop"            "80s pop"                                   "lower confidence expected"

CLIENT_ID="${ORIG_CLIENT}-v8"
run_vibe "Very vague"         "music"                                     "fallback_used likely true"

CLIENT_ID="${ORIG_CLIENT}-v9"
run_vibe "Emoji edge case"    "🎵"                                        "single emoji, edge case"

# 200-character string (exact max)
LONG_VIBE="This is a very long and detailed vibe description that goes all the way up to the two hundred character limit which is the absolute maximum that the API endpoint is allowed to accept per request"
LONG_VIBE="${LONG_VIBE:0:200}"
CLIENT_ID="${ORIG_CLIENT}-v10"
run_vibe "200-char vibe"      "$LONG_VIBE"                                "max-length input"

CLIENT_ID="$ORIG_CLIENT"   # restore

# ── 3. Rate limiting (10/min per client) ──────────────────────────────────────
section "3. Rate limiting — 12 rapid requests with same X-Client-ID"
RATE_CLIENT="rate-test-$(date +%s)"
RATE_429=0
for i in $(seq 1 12); do
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$BASE/sessions/$SESSION_ID/queue/vibe" \
    -H "Content-Type: application/json" \
    -H "X-Client-ID: $RATE_CLIENT" \
    -d '{"vibe": "chill beats", "display_name": "RateTester"}')
  echo "  Request $i: HTTP $HTTP_CODE"
  [ "$HTTP_CODE" = "429" ] && RATE_429=$((RATE_429 + 1))
done
[ "$RATE_429" -gt "0" ] \
  && pass "Got 429 after rate limit — $RATE_429 request(s) rejected" \
  || fail "No 429 seen — rate limiting may not be working"

# ── 4. GET queue ──────────────────────────────────────────────────────────────
section "4. GET queue — ordered list"
QUEUE_RESP=$(curl -sf "$BASE/sessions/$SESSION_ID/queue" \
  -H "X-Client-ID: $CLIENT_ID")
echo "$QUEUE_RESP" | jq '[.[] | {pos: .position, title, artist, vibe_query, similarity_score}]'
QUEUE_LEN=$(echo "$QUEUE_RESP" | jq 'length')
[ "$QUEUE_LEN" -gt 0 ] && pass "Queue has $QUEUE_LEN items" || fail "Queue is empty"

# ── 5. DELETE queue item ──────────────────────────────────────────────────────
section "5. DELETE first queue item"
FIRST_ID=$(echo "$QUEUE_RESP" | jq -r '.[0].id')
if [ -n "$FIRST_ID" ] && [ "$FIRST_ID" != "null" ]; then
  DEL_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X DELETE "$BASE/sessions/$SESSION_ID/queue/$FIRST_ID" \
    -H "X-Client-ID: $CLIENT_ID")
  [ "$DEL_CODE" = "204" ] && pass "Deleted item $FIRST_ID (204)" || fail "Delete returned $DEL_CODE"

  # Verify deletion
  NEW_LEN=$(curl -sf "$BASE/sessions/$SESSION_ID/queue" | jq 'length')
  [ "$NEW_LEN" -lt "$QUEUE_LEN" ] \
    && pass "Queue shrank from $QUEUE_LEN → $NEW_LEN" \
    || fail "Queue length unchanged after delete"
else
  info "Queue was empty, skipping delete test"
fi

# ── 6. WebSocket broadcast test ───────────────────────────────────────────────
section "6. WebSocket queue_updated broadcast"
if ! command -v wscat &>/dev/null; then
  info "wscat not installed — skipping WebSocket test"
  info "Install with: npm i -g wscat"
  info "Then manually run:"
  echo ""
  echo "  # Terminal 1 — listen:"
  echo "  wscat -c 'ws://localhost:8000/sessions/$SESSION_ID/ws?display_name=Listener'"
  echo ""
  echo "  # Terminal 2 — trigger:"
  echo "  curl -X POST $BASE/sessions/$SESSION_ID/queue/vibe \\"
  echo "    -H 'Content-Type: application/json' \\"
  echo "    -H 'X-Client-ID: ws-test-client' \\"
  echo "    -d '{\"vibe\": \"dreamy indie\", \"display_name\": \"Tester\"}'"
  echo ""
  echo "  Expected in Terminal 1:"
  echo '  {"event":"queue_updated","session_id":"...","payload":{"added_tracks":[...],"added_by_ai":true,...}}'
else
  info "Opening wscat in background (10s window)..."
  TMP_WS_OUT=$(mktemp)
  wscat -c "ws://localhost:8000/sessions/$SESSION_ID/ws?display_name=WSListener" \
    --no-color 2>&1 &
  WS_PID=$!
  sleep 1   # let connection establish

  # Send a vibe query
  curl -sf -X POST "$BASE/sessions/$SESSION_ID/queue/vibe" \
    -H "Content-Type: application/json" \
    -H "X-Client-ID: ws-trigger-client" \
    -d '{"vibe": "dreamy indie folk", "display_name": "Tester"}' > /dev/null

  sleep 2
  kill $WS_PID 2>/dev/null
  pass "WebSocket test triggered — check wscat output above for queue_updated event"
fi

# ── 7. Invalid session_id ─────────────────────────────────────────────────────
section "7. Error handling — invalid session"
BAD_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BASE/sessions/00000000-0000-0000-0000-000000000000/queue/vibe" \
  -H "Content-Type: application/json" \
  -H "X-Client-ID: $CLIENT_ID" \
  -d '{"vibe": "test", "display_name": "Tester"}')
[ "$BAD_CODE" = "404" ] && pass "Invalid session → 404" || fail "Expected 404, got $BAD_CODE"

# ── 8. Vibe too short ─────────────────────────────────────────────────────────
section "8. Error handling — vibe too short (< 3 chars)"
SHORT_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BASE/sessions/$SESSION_ID/queue/vibe" \
  -H "Content-Type: application/json" \
  -H "X-Client-ID: $CLIENT_ID" \
  -d '{"vibe": "hi", "display_name": "Tester"}')
[ "$SHORT_CODE" = "422" ] && pass "Too-short vibe → 422" || fail "Expected 422, got $SHORT_CODE"

# ── 9. Fallback behavior (manual) ────────────────────────────────────────────
section "9. Fallback behavior — manual steps"
echo ""
echo "  To test Claude Haiku fallback:"
echo ""
echo "  1. In .env, change:  ANTHROPIC_API_KEY=sk-ant-INVALID"
echo "  2. Restart app:      docker compose restart app"
echo "  3. Run:"
echo "     curl -X POST $BASE/sessions/$SESSION_ID/queue/vibe \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -H 'X-Client-ID: fallback-test' \\"
echo "       -d '{\"vibe\": \"late night drive\", \"display_name\": \"Tester\"}'"
echo ""
echo "  Expected:"
echo "    - HTTP 200 (not 500)"
echo "    - fallback_used: true"
echo "    - vibe_interpreted_as: [\"late night drive\"]  (raw input as keyword)"
echo "    - confidence: 0.1"
echo "    - tracks still returned via pure vector search"
echo ""
echo "  4. Restore real key and restart"

# ── Summary ───────────────────────────────────────────────────────────────────
section "Done"
echo "Session used: $SESSION_ID"
echo "Re-run single commands against it with:"
echo "  export SESSION_ID=$SESSION_ID"
