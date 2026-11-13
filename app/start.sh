#!/usr/bin/env sh

# JellyJams Container Startup Script
echo "🎵 Starting JellyJams Generator..."

# Function to run playlist generator in background
run_generator() {
    echo "🎯 Starting playlist generator background process..."
    python /app/vibecodeplugin.py &
    GENERATOR_PID=$!
    echo "📊 Playlist generator started with PID: $GENERATOR_PID"
}

# Check if web UI is enabled
if [ "$ENABLE_WEB_UI" = "true" ]; then
    echo "🌐 Web UI enabled - starting both web UI and playlist generator"
    
    # Start playlist generator in background
    run_generator
    
    # Start the web application. One process: the generator writes /data/run_status.json
    # and the web UI reads it, so they must share the filesystem, not a worker each.
    echo "🌐 Starting web UI on port ${WEB_PORT}"
    exec python /app/webapp.py
else
    echo "🎯 Web UI disabled - running playlist generator only"
    
    # Run the original playlist generator
    exec python /app/vibecodeplugin.py
fi
