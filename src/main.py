"""Flask application factory and page routes for FantAsta."""

import os

from flask import Flask, render_template

from bid_manager import bid_bp
from notes_manager import notes_bp


def create_app() -> Flask:
    """Create and configure the FantAsta web application."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get(
        "FLASK_SECRET_KEY",
        "local-development-key-change-before-deployment",
    )
    app.register_blueprint(bid_bp)
    app.register_blueprint(notes_bp)

    @app.get("/")
    def home():
        """Render the tool selection dashboard."""
        return render_template("index.html")

    @app.get("/bid-manager")
    def bid_manager():
        """Render the fantasy-football auction manager."""
        return render_template("bid-manager.html")

    @app.get("/notes-manager")
    def notes_manager():
        """Render the player scouting notes manager."""
        return render_template("notes-manager.html")

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
