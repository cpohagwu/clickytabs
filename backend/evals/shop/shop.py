"""A tiny scripted shop for the browser-task acceptance test (standard library only).

    python evals/shop/shop.py --port 8123          then map shop.test to it in Chrome:
                                                   --host-resolver-rules="MAP shop.test 127.0.0.1:8123"

Sign in as alice / mugs4all (a session cookie, HttpOnly, SameSite=Lax). Products have "Add to cart" buttons; the
cart needs the session; "Place order" (POST /checkout) is the irreversible step. GET /_state returns every user's cart
and orders as JSON so a test can check what happened; POST /_reset empties them.
"""

from __future__ import annotations

import argparse
import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PRODUCTS = {"blue-mug": ("Blue Mug", 12.0), "red-mug": ("Red Mug", 14.0), "green-mug": ("Green Mug", 10.0)}
USERS = {"alice": "mugs4all"}
LOCK = threading.Lock()
SESSIONS: dict[str, str] = {}
STATE: dict[str, dict[str, list[str] | list[dict[str, object]]]] = {
    user: {"cart": [], "orders": []} for user in USERS
}

STYLE = """<style>body{font:16px system-ui,sans-serif;margin:0;background:#f6f4ef;color:#222}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 28px;background:#2b3a55;color:#fff}
header a{color:#fff;margin-left:16px}main{padding:24px 28px}.grid{display:flex;gap:18px;flex-wrap:wrap}
.card{background:#fff;border-radius:12px;padding:16px;width:200px;box-shadow:0 2px 8px #0002}
button{font:inherit;padding:8px 14px;border-radius:8px;border:0;background:#e07a2e;color:#fff;cursor:pointer}
input{font:inherit;padding:6px;margin:4px 0;display:block}</style>"""


def page(title: str, user: str | None, body: str) -> bytes:
    who = (f'Signed in as {html.escape(user)} <a href="/cart">Cart</a>' if user
           else '<a href="/login">Sign in</a>')
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(title)} - Mug Shop"
            f"</title>{STYLE}</head><body><header><a href='/' style='margin:0;font-weight:700'>Mug Shop</a>"
            f"<span>{who}</span></header><main><h1>{html.escape(title)}</h1>{body}</main></body></html>").encode()


class Shop(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:      # quiet
        return

    def user(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "session":
                with LOCK:
                    return SESSIONS.get(value)
        return None

    def send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8",
             headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, to: str, headers: dict[str, str] | None = None) -> None:
        self.send(303, b"", headers={"Location": to, **(headers or {})})

    def form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}

    def do_GET(self) -> None:
        user = self.user()
        if self.path == "/_state":
            with LOCK:
                self.send(200, json.dumps(STATE).encode(), "application/json")
            return
        if self.path == "/login":
            body = ("<form method='post' action='/login'><label>Username<input name='username'></label>"
                    "<label>Password<input name='password' type='password'></label><button>Sign in</button></form>")
            self.send(200, page("Sign in", user, body))
            return
        if self.path == "/cart":
            if not user:
                self.redirect("/login")
                return
            with LOCK:
                cart = list(STATE[user]["cart"])        # type: ignore[arg-type]
                orders = list(STATE[user]["orders"])
            items = "".join(f"<li>{html.escape(PRODUCTS[i][0])} - ${PRODUCTS[i][1]:.2f}</li>" for i in cart)
            total = sum(PRODUCTS[i][1] for i in cart)
            body = (f"<ul id='cart'>{items or '<li>Your cart is empty.</li>'}</ul><p>Total: ${total:.2f}</p>"
                    + ("<form method='post' action='/checkout'><button>Place order</button></form>" if cart else "")
                    + f"<p>Orders placed: {len(orders)}</p>")
            self.send(200, page("Your cart", user, body))
            return
        cards = "".join(
            f"<div class='card'><h2>{html.escape(name)}</h2><p>${price:.2f}</p>"
            f"<form method='post' action='/cart/add'><input type='hidden' name='item' value='{key}'>"
            f"<button>Add to cart</button></form></div>"
            for key, (name, price) in PRODUCTS.items())
        self.send(200, page("Mugs", user, f"<div class='grid'>{cards}</div>"))

    def do_POST(self) -> None:
        user = self.user()
        data = self.form()
        if self.path == "/_reset":
            with LOCK:
                for state in STATE.values():
                    state["cart"].clear()
                    state["orders"].clear()
            self.send(200, b"{}", "application/json")
            return
        if self.path == "/login":
            if USERS.get(data.get("username", "")) != data.get("password"):
                self.send(401, page("Sign in", None, "<p>Wrong username or password.</p>"))
                return
            token = secrets.token_urlsafe(24)
            with LOCK:
                SESSIONS[token] = data["username"]
            self.redirect("/", {"Set-Cookie": f"session={token}; Path=/; HttpOnly; SameSite=Lax"})
            return
        if not user:
            self.redirect("/login")
            return
        if self.path == "/cart/add" and data.get("item") in PRODUCTS:
            with LOCK:
                STATE[user]["cart"].append(data["item"])       # type: ignore[union-attr]
            self.redirect("/cart")
            return
        if self.path == "/checkout":
            with LOCK:
                cart = list(STATE[user]["cart"])               # type: ignore[arg-type]
                if cart:
                    STATE[user]["orders"].append({"items": cart, "total": sum(PRODUCTS[i][1] for i in cart)})
                    STATE[user]["cart"].clear()                # type: ignore[union-attr]
            self.send(200, page("Thank you", user, f"<p>Order placed: {len(cart)} item(s).</p>"))
            return
        self.send(404, page("Not found", user, ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Shop)
    print(f"shop on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
