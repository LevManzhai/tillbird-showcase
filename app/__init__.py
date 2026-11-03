from pathlib import Path

from flask import Flask
from sqlalchemy import event
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import AppConfig
from app.db_init import add_missing_columns_safely, create_all_tables_safely
from app.extensions import db
from app.net import apply_outbound_ip_family


def create_app(config: AppConfig | None = None) -> Flask:
    app = Flask(__name__)
    config = config or AppConfig()
    apply_outbound_ip_family(config.outbound_ip_family)

    app.config["SECRET_KEY"] = config.secret_key
    app.config["SQLALCHEMY_DATABASE_URI"] = config.database_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.extensions["app_config"] = config

    if config.trusted_proxy_count > 0:
        # Makes request.scheme/request.host (and therefore
        # app.routes.chat._own_origin()) reflect X-Forwarded-Proto/Host from
        # a reverse proxy (e.g. Caddy) instead of the raw internal
        # connection — otherwise the own-origin CORS exemption for our demo
        # pages would compare against "http://internal-host:port" and never
        # match the real "https://public-domain" the browser sent.
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=config.trusted_proxy_count,
            x_proto=config.trusted_proxy_count,
            x_host=config.trusted_proxy_count,
            x_port=config.trusted_proxy_count,
            x_prefix=config.trusted_proxy_count,
        )

    if config.database_uri.startswith("sqlite:///") and not config.database_uri.endswith(":memory:"):
        db_path = Path(config.database_uri.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)

    db.init_app(app)

    from app import models  # noqa: F401  (registers tables with db.metadata)
    from app.routes.chat import chat_bp
    from app.routes.health import health_bp
    from app.routes.leads import leads_bp
    from app.routes.oauth import oauth_bp
    from app.routes.widget import widget_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(leads_bp)
    app.register_blueprint(widget_bp)
    app.register_blueprint(oauth_bp)

    with app.app_context():
        if config.database_uri.startswith("sqlite:///"):
            # gunicorn runs multiple worker *processes* (see Dockerfile), all
            # opening the same SQLite file. Default SQLite locking serialises
            # writers and can raise "database is locked" under concurrent
            # requests; WAL mode lets readers and a writer proceed together,
            # and busy_timeout makes a writer wait briefly instead of erroring
            # immediately if another write is briefly in progress.
            @event.listens_for(db.engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

        create_all_tables_safely(db)
        add_missing_columns_safely(db)

    return app
