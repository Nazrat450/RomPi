#!/bin/bash
# Download and extract AriaNg to serve locally

cd /opt/rompi
mkdir -p static/ariang
cd static/ariang

echo "Downloading latest AriaNg..."
# Get the latest release URL
LATEST_URL=$(curl -s https://api.github.com/repos/mayswind/AriaNg/releases/latest | grep "browser_download_url.*AllInOne.zip" | cut -d '"' -f 4)

if [ -z "$LATEST_URL" ]; then
    echo "Failed to get latest release URL, trying direct download..."
    wget -q https://github.com/mayswind/AriaNg/releases/latest/download/AriaNg-AllInOne.zip -O ariang.zip
else
    echo "Found latest release: $LATEST_URL"
    wget -q "$LATEST_URL" -O ariang.zip
fi

if [ $? -eq 0 ] && [ -f ariang.zip ]; then
    echo "Extracting AriaNg..."
    unzip -q -o ariang.zip
    rm ariang.zip
    echo "AriaNg downloaded successfully!"
    echo "Files are in: /opt/rompi/static/ariang"
    ls -la
else
    echo "Failed to download AriaNg"
    exit 1
fi

