"""Servidor de preview da interface web — não requer token do Telegram."""
import sys
sys.path.insert(0, ".")

from web import create_app

app = create_app()
app.jinja_env.auto_reload = True
app.config["TEMPLATES_AUTO_RELOAD"] = True

if __name__ == "__main__":
    print("Dashboard disponível em: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
