from app import create_app
import os

app = create_app()

if __name__ == "__main__":
    use_adhoc_ssl = os.getenv("CCM_SSL_ADHOC", "").lower() in {"1", "true", "yes", "on"}
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        ssl_context=None#"adhoc" #if use_adhoc_ssl else None,
    )

