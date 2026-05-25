"""Flask admin UI for fb-content-engine.

Auth: Google OAuth (reuses setformoney.com OAuth project credentials), restricted
to ADMIN_ALLOWED_EMAILS allowlist.

Dashboard: shows today's posts (= the batch generated yesterday for today's
publish date), with copy-to-clipboard buttons + mark-as-posted tracking
persisted in SQLite.

Run locally (from project root):
    .venv/bin/python -m admin.app

Run in production (VPS):
    .venv/bin/gunicorn -w 2 -b 127.0.0.1:5001 admin.app:app
"""
import datetime
import os
import pathlib
import sqlite3
from functools import wraps

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

from admin import posts as posts_lib
from admin import status as status_lib

# --- Setup ---
ROOT = pathlib.Path(__file__).resolve().parent.parent  # fb-content-engine/
load_dotenv(ROOT / ".env")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ["FLASK_SECRET_KEY"]
app.config["SESSION_COOKIE_SECURE"] = True  # HTTPS only in prod
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ALLOWED_EMAILS = {
    e.strip().lower() for e in os.environ.get("ADMIN_ALLOWED_EMAILS", "").split(",") if e.strip()
}
OAUTH_REDIRECT_URI = os.environ.get(
    "ADMIN_OAUTH_REDIRECT_URI", "https://fb.setformoney.com/auth/callback"
)

# --- Google OAuth ---
oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# --- SQLite for post status tracking ---
DB_PATH = ROOT / "admin" / "admin.db"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_status (
            niche TEXT NOT NULL,
            target_date TEXT NOT NULL,
            post_id TEXT NOT NULL,
            posted_at TIMESTAMP,
            posted_by TEXT,
            PRIMARY KEY (niche, target_date, post_id)
        )
    """)
    conn.commit()
    conn.close()


# --- Auth helpers ---
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_email" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.before_request
def add_security_headers():
    # Set up — actually headers go AFTER, see after_request
    pass


@app.after_request
def security_headers(resp):
    # Multi-layer noindex defense
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


# --- Routes ---
@app.route("/")
@login_required
def dashboard():
    # Default to the first niche (only one for now). Future: niche switcher.
    niches = posts_lib.list_niches(ROOT)
    if not niches:
        return render_template("dashboard.html",
                               user=session["user_email"],
                               today_date=posts_lib.today_iso(),
                               tomorrow_date=posts_lib.tomorrow_iso(),
                               today_posts=[],
                               tomorrow_posts=[],
                               niche=None)
    niche = niches[0]
    today = posts_lib.today_iso()
    tomorrow = posts_lib.tomorrow_iso()
    conn = get_db()
    today_status = status_lib.get_status_lookup(conn, niche, today)
    tomorrow_status = status_lib.get_status_lookup(conn, niche, tomorrow)
    today_posts = posts_lib.load_posts(ROOT, niche, today, today_status)
    tomorrow_posts = posts_lib.load_posts(ROOT, niche, tomorrow, tomorrow_status)
    return render_template("dashboard.html",
                           user=session["user_email"],
                           today_date=today,
                           tomorrow_date=tomorrow,
                           today_posts=today_posts,
                           tomorrow_posts=tomorrow_posts,
                           niche=niche)


@app.route("/image/<niche>/<target_date>/<filename>")
@login_required
def serve_image(niche, target_date, filename):
    # Path traversal defense: validate components don't contain ".."
    for part in (niche, target_date, filename):
        if ".." in part or "/" in part:
            abort(400)
    img_dir = ROOT / "niches" / niche / "out" / target_date / "images"
    return send_from_directory(img_dir, filename)


@app.route("/mark-posted", methods=["POST"])
@login_required
def mark_posted():
    data = request.get_json() or request.form
    niche = data.get("niche")
    target_date = data.get("target_date")
    post_id = data.get("post_id")
    action = data.get("action", "mark")  # "mark" or "unmark"
    if not all([niche, target_date, post_id]):
        return jsonify({"ok": False, "error": "missing fields"}), 400
    conn = get_db()
    if action == "unmark":
        status_lib.unmark_posted(conn, niche, target_date, post_id)
        return jsonify({"ok": True, "action": "unmarked"})
    status_lib.mark_posted(conn, niche, target_date, post_id, session["user_email"])
    return jsonify({"ok": True, "action": "marked", "posted_by": session["user_email"]})


@app.route("/login")
def login():
    if "user_email" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/auth/google")
def auth_google():
    return oauth.google.authorize_redirect(OAUTH_REDIRECT_URI)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.parse_id_token(token, None)
    email = (userinfo.get("email") or "").lower()
    if not email:
        abort(403, "No email returned from Google.")
    if email not in ALLOWED_EMAILS:
        return render_template("error.html",
                               title="Access denied",
                               message=f"The email {email} is not authorized for this admin. "
                                       f"If you believe this is an error, contact the owner."), 403
    session["user_email"] = email
    session["user_name"] = userinfo.get("name", "")
    session["user_picture"] = userinfo.get("picture", "")
    next_url = request.args.get("next") or url_for("dashboard")
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dev-login")
def dev_login():
    """Local-only bypass for testing without Google OAuth. Disabled in production."""
    if not app.debug:
        abort(404)
    # Use the first allowlisted email as the test identity
    test_email = next(iter(ALLOWED_EMAILS), "dev@local")
    session["user_email"] = test_email
    session["user_name"] = "Dev User"
    session["user_picture"] = ""
    return redirect(url_for("dashboard"))


@app.route("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "fb-content-engine-admin"})


# --- Local dev entry ---
if __name__ == "__main__":
    init_db()
    # Dev mode — over HTTP for localhost. In prod, gunicorn behind nginx handles HTTPS.
    app.config["SESSION_COOKIE_SECURE"] = False  # allow HTTP for local dev
    app.run(host="127.0.0.1", port=5001, debug=True)
