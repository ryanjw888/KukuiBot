#!/bin/bash
# KukuiBot Password Reset — double-click to run
cd "$(dirname "$0")"
python3 reset-password.py
echo ""
echo "Press any key to close..."
read -n 1
