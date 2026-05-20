#!/bin/bash
# PhysioIQ PWA — Quick Deploy Script
# Run this from the PhysioIQ-PWA folder

echo "🔧 Setting up PhysioIQ PWA..."

# Initialize git repo
git init
git checkout -b main

# Add all files except .env (contains API key)
echo ".env" >> .gitignore
echo "physioiq.db" >> .gitignore
echo "__pycache__/" >> .gitignore
echo ".garmin_tokens/" >> .gitignore
git add .
git commit -m "PhysioIQ PWA - initial deploy"

# Push to your existing GitHub repo (new branch to not overwrite old code)
git remote add origin https://github.com/rrsuare/physioiq.git 2>/dev/null
git push -u origin main --force

echo ""
echo "✅ Code pushed to GitHub!"
echo ""
echo "Next: Go to https://railway.app and deploy from this repo."
