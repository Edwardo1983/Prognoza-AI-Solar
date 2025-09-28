Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Set-Location -Path (Join-Path $PSScriptRoot '..')

python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
