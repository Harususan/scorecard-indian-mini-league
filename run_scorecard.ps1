# Launch the separate scorecard app using its own dark theme and port.
Set-Location -LiteralPath $PSScriptRoot
python -m streamlit run scorecard_app.py --server.port 8502 --theme.base dark --theme.primaryColor "#1c9f6d" --theme.backgroundColor "#090e16" --theme.secondaryBackgroundColor "#101722" --theme.textColor "#eaf0f8"
