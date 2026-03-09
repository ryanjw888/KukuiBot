@echo off
:: KukuiBot Windows Installer Launcher
:: Double-click this file or run from CMD to install KukuiBot.
:: Downloads and runs install.ps1 via PowerShell with appropriate permissions.

powershell -ExecutionPolicy Bypass -Command "Invoke-RestMethod https://github.com/ryanjw888/KukuiBot/raw/main/install.ps1 | Invoke-Expression"
pause
