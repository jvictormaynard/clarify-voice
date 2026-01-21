#!/bin/bash

# ClarifyVoice - Linux start script

# Check for required dependencies
check_dependency() {
    if ! command -v "$1" &> /dev/null; then
        echo "Error: $1 is not installed."
        echo "Install with: $2"
        return 1
    fi
    return 0
}

echo "Checking dependencies..."

# Check for sox
if ! check_dependency "sox" "sudo apt install sox libsox-fmt-all"; then
    exit 1
fi

# Check for xdotool (required for paste functionality)
if ! check_dependency "xdotool" "sudo apt install xdotool"; then
    echo "Warning: xdotool not found. Paste functionality will not work."
fi

# Check for Node.js
if ! check_dependency "node" "sudo apt install nodejs"; then
    exit 1
fi

# Check for npm
if ! check_dependency "npm" "sudo apt install npm"; then
    exit 1
fi

echo "All dependencies found. Starting ClarifyVoice..."

# Run the app
npm start
