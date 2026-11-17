@echo off
rem Сборка WORKS_RU_Installer.exe (требуется Python 3.10.1+ / 3.12 и pyinstaller)
cd /d "%~dp0.."
python -m PyInstaller --onefile --noconsole --name WORKS_RU_Installer ^
  --add-data "src\data\ru.json;data" --paths "src" ^
  --exclude-module matplotlib --exclude-module scipy --exclude-module pandas --exclude-module numpy --exclude-module PIL --exclude-module IPython --exclude-module pytest ^
  src\works_ru_installer.py
echo Готово: dist\WORKS_RU_Installer.exe
