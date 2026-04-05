#!/bin/bash
# =============================================================
# Everything Downloader — One-command cookie setup for Render
# =============================================================
# This script:
#   1. Exports YouTube cookies from your browser
#   2. Base64 encodes them
#   3. Sets the YT_COOKIES env var on Render via their API
#
# Prerequisites:
#   - yt-dlp installed locally (brew install yt-dlp)
#   - You're logged into YouTube in your browser
#   - Render API key (get it from https://dashboard.render.com/account)
# =============================================================

set -e

echo ""
echo "🔧 Everything Downloader — Cookie Setup"
echo "========================================"
echo ""

# Step 1: Export cookies
echo "Step 1: Exporting YouTube cookies from your browser..."
echo ""
echo "Which browser are you logged into YouTube with?"
echo "  1) Chrome"
echo "  2) Firefox"
echo "  3) Safari"
echo "  4) Edge"
echo "  5) Brave"
echo ""
read -p "Enter number (1-5): " browser_choice

case $browser_choice in
    1) BROWSER="chrome" ;;
    2) BROWSER="firefox" ;;
    3) BROWSER="safari" ;;
    4) BROWSER="edge" ;;
    5) BROWSER="brave" ;;
    *) echo "Invalid choice"; exit 1 ;;
esac

COOKIE_FILE="/tmp/yt_cookies_export.txt"

echo ""
echo "Extracting cookies from $BROWSER..."
yt-dlp --cookies-from-browser "$BROWSER" --cookies "$COOKIE_FILE" --skip-download "https://www.youtube.com" 2>/dev/null || {
    echo "❌ Failed to extract cookies. Make sure:"
    echo "   - yt-dlp is installed (brew install yt-dlp)"
    echo "   - You're logged into YouTube in $BROWSER"
    echo "   - $BROWSER is closed (some browsers lock the cookie DB)"
    exit 1
}

if [ ! -s "$COOKIE_FILE" ]; then
    echo "❌ Cookie file is empty. Make sure you're logged into YouTube."
    exit 1
fi

echo "✅ Cookies exported successfully!"

# Step 2: Base64 encode
echo ""
ENCODED=$(base64 -i "$COOKIE_FILE" | tr -d '\n')
echo "✅ Cookies encoded (${#ENCODED} chars)"

# Step 3: Ask how to apply
echo ""
echo "How do you want to set the cookies?"
echo "  1) Copy to clipboard (paste manually in Render dashboard)"
echo "  2) Set via Render API (automatic)"
echo ""
read -p "Enter number (1-2): " setup_choice

if [ "$setup_choice" = "1" ]; then
    echo "$ENCODED" | pbcopy 2>/dev/null || {
        echo ""
        echo "Couldn't copy to clipboard. Here's the value (it's long):"
        echo ""
        echo "$ENCODED"
        echo ""
    }
    echo ""
    echo "✅ Copied to clipboard!"
    echo ""
    echo "Now go to Render dashboard:"
    echo "  1. Open https://dashboard.render.com"
    echo "  2. Click your Everything service"
    echo "  3. Go to Environment tab"
    echo "  4. Add variable: YT_COOKIES = (Cmd+V to paste)"
    echo "  5. Save & redeploy"
    echo ""
elif [ "$setup_choice" = "2" ]; then
    echo ""
    read -p "Enter your Render API key: " RENDER_API_KEY
    read -p "Enter your Render service ID (starts with srv-): " SERVICE_ID
    echo ""
    echo "Setting YT_COOKIES on Render..."
    
    curl -s -X PUT "https://api.render.com/v1/services/$SERVICE_ID/env-vars/YT_COOKIES" \
        -H "Authorization: Bearer $RENDER_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"value\": \"$ENCODED\"}" > /dev/null && {
        echo "✅ Environment variable set!"
        echo ""
        echo "Now deploy your service to apply changes:"
        echo "  curl -X POST https://api.render.com/v1/services/$SERVICE_ID/deploys -H 'Authorization: Bearer $RENDER_API_KEY'"
    } || {
        echo "❌ Failed to set via API. Try option 1 (clipboard) instead."
    }
fi

# Cleanup
rm -f "$COOKIE_FILE"
echo ""
echo "Done! 🎉"
