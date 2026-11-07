@echo off
cd /d "%~dp0"
python news_bulletin.py >> news.log 2>&1
