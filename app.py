from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import sqlite3, json, csv, io, html, os, secrets, hashlib, hmac, time, re
from datetime import datetime

BASE = Path(__file__).resolve().parent
DB = BASE / "ziestore.db"
HOST = os.environ.get("ZIESTORE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ZIESTORE_PORT", "8080"))
SESSIONS = {}

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS branches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sku TEXT UNIQUE, brand TEXT NOT NULL, model TEXT NOT NULL,
          variant TEXT DEFAULT '', condition TEXT NOT NULL DEFAULT 'Baru',
          min_stock INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS stock_units (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          product_id INTEGER NOT NULL REFERENCES products(id),
          branch_id INTEGER NOT NULL REFERENCES branches(id),
          imei1 TEXT NOT NULL UNIQUE, imei2 TEXT UNIQUE,
          serial_number TEXT DEFAULT '',
          cost_price INTEGER NOT NULL CHECK(cost_price >= 0),
          selling_price INTEGER NOT NULL CHECK(selling_price >= 0),
          status TEXT NOT NULL DEFAULT 'Tersedia'
            CHECK(status IN ('Tersedia','Terjual','Retur','Rusak')),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sales (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          receipt_no TEXT NOT NULL UNIQUE,
          branch_id INTEGER NOT NULL REFERENCES branches(id),
          customer_name TEXT DEFAULT '',
          payment_method TEXT NOT NULL,
          total INTEGER NOT NULL CHECK(total >= 0),
          total_cost INTEGER NOT NULL CHECK(total_cost >= 0),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sale_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sale_id INTEGER NOT NULL REFERENCES sales(id),
          stock_unit_id INTEGER NOT NULL UNIQUE REFERENCES stock_units(id),
          imei_snapshot TEXT NOT NULL,
          item_price INTEGER NOT NULL,
          cost_snapshot INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('owner','cashier')),
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER,
          action TEXT NOT NULL, detail TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO branches(code, name) VALUES ('PUSAT','ZIESTORE - Cabang Utama');
        """)
        # Upgrade database lama dari Tahap 1 tanpa menghapus data yang sudah ada.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(audit_logs)").fetchall()}
        if "user_id" not in cols:
            c.execute("ALTER TABLE audit_logs ADD COLUMN user_id INTEGER")

def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return salt.hex() + "$" + digest.hex()

def password_ok(password, stored):
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False

def user_count():
    with db() as c: return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]

def current_user(handler):
    token = ""
    for part in handler.headers.get("Cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "ziestore_session": token = v
    info = SESSIONS.get(token)
    if not info or info["expires"] < time.time():
        SESSIONS.pop(token, None); return None
    return info

def login_page(message=""):
    extra = f"<div class='notice error'>{esc(message)}</div>" if message else ""
    if user_count() == 0:
        heading, action, fields = "Buat akun Owner pertama", "/setup", "<label>Nama pengguna<input name='username' required minlength='4' maxlength='40' autocomplete='username'></label><label>Kata sandi (minimal 12 karakter)<input type='password' name='password' required minlength='12' autocomplete='new-password'></label><label>Ulangi kata sandi<input type='password' name='password2' required minlength='12'></label>"
        button = "Buat akun Owner"
    else:
        heading, action, fields, button = "Masuk ke ZIESTORE", "/login", "<label>Nama pengguna<input name='username' required autocomplete='username'></label><label>Kata sandi<input type='password' name='password' required autocomplete='current-password'></label>", "Masuk"
    body = f"<div class='login-card'><div class='brand'><span class='mark'>Z</span><div><b>ZIESTORE</b><small>GADGET STORE MANAGEMENT</small></div></div><h1>{heading}</h1><p class='muted'>Gunakan kata sandi unik dan jangan membagikannya.</p>{extra}<form method='post' action='{action}' class='form'>{fields}<button>{button}</button></form></div>"
    return page(body, "Login ZIESTORE", logged_in=False)

def money(n):
    return "Rp" + f"{int(n):,}".replace(",", ".")

def esc(s):
    return html.escape(str(s if s is not None else ""))

def page(body, title="ZIESTORE", user=None, logged_in=True):
    if user and logged_in:
        nav = "<nav><a href='/'>Dashboard</a><a href='/products'>Produk & IMEI</a><a href='/sales'>Kasir</a><a href='/reports'>Laporan</a><a href='/import-export'>Impor / Ekspor</a>" + ("<a href='/users'>Pengguna</a>" if user["role"] == "owner" else "") + "</nav>"
        userbar = f"<div class='userbar'><span>Masuk sebagai <b>{esc(user['username'])}</b> · {'Owner' if user['role']=='owner' else 'Kasir'}</span><form method='post' action='/logout'><button class='logout'>Keluar</button></form></div>"
    else:
        nav, userbar = "", ""
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title><link rel="stylesheet" href="/static/style.css"></head><body><header><div class="brand"><span class="mark">Z</span><div><b>ZIESTORE</b><small>GADGET STORE MANAGEMENT</small></div></div>{nav}</header>{userbar}<main>{body}</main><footer>ZIESTORE Tahap 2 • Login & hak akses dasar • Backup database secara berkala</footer></body></html>"""

def layout(title, subtitle, content):
    return f'<div class="heading"><div><p class="eyebrow">ZIESTORE MANAGEMENT</p><h1>{title}</h1><p class="muted">{subtitle}</p></div></div>{content}'

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, body, status=200, content_type="text/html; charset=utf-8", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status); self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(data)
    def redirect(self, path):
        self.send(b"", 303, headers={"Location":path})
    def body_data(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return parse_qs(raw, keep_blank_values=True)
    def val(self, data, key, default=""):
        return (data.get(key) or [default])[0].strip()
    def do_GET(self):
        p = urlparse(self.path).path
        if p.startswith("/static/"):
            file = BASE / p.lstrip("/")
            if file.exists() and file.is_file():
                typ = "text/css; charset=utf-8" if file.suffix == ".css" else "application/octet-stream"
                return self.send(file.read_bytes(), content_type=typ)
            return self.send("Tidak ditemukan",404)
        if p in ("/login", "/setup"):
            if p == "/setup" and user_count() > 0: return self.redirect("/login")
            if p == "/login" and user_count() == 0: return self.redirect("/setup")
            return self.send(login_page())
        user = current_user(self)
        if not user: return self.redirect("/setup" if user_count() == 0 else "/login")
        if p == "/":
            with db() as c:
                units = c.execute("SELECT COUNT(*) n FROM stock_units WHERE status='Tersedia'").fetchone()["n"]
                value = c.execute("SELECT COALESCE(SUM(cost_price),0) n FROM stock_units WHERE status='Tersedia'").fetchone()["n"]
                sales = c.execute("SELECT COALESCE(SUM(total),0) n FROM sales WHERE date(created_at)=date('now','localtime')").fetchone()["n"]
                profit = c.execute("SELECT COALESCE(SUM(total-total_cost),0) n FROM sales WHERE date(created_at)=date('now','localtime')").fetchone()["n"]
                recent = c.execute("""SELECT s.*, b.name branch FROM sales s JOIN branches b ON b.id=s.branch_id ORDER BY s.id DESC LIMIT 8""").fetchall()
            rows = "".join(f"<tr><td>{esc(r['receipt_no'])}</td><td>{esc(r['branch'])}</td><td>{esc(r['created_at'])}</td><td>{money(r['total'])}</td><td>{money(r['total']-r['total_cost'])}</td></tr>" for r in recent)
            content = f"""<div class="cards">
<div class="metric"><span>Unit tersedia</span><strong>{units}</strong><small>Stok berdasarkan IMEI</small></div>
<div class="metric"><span>Nilai modal stok</span><strong>{money(value)}</strong><small>Unit dengan status tersedia</small></div>
<div class="metric"><span>Omzet hari ini</span><strong>{money(sales)}</strong><small>Transaksi tanggal hari ini</small></div>
<div class="metric"><span>Laba kotor hari ini</span><strong>{money(profit)}</strong><small>Omzet dikurangi modal unit</small></div></div>
<div class="panel"><div class="panel-title"><h2>Transaksi terbaru</h2><a class="textlink" href="/reports">Lihat laporan →</a></div>
<div class="tablewrap"><table><thead><tr><th>Struk</th><th>Cabang</th><th>Tanggal</th><th>Total</th><th>Laba kotor</th></tr></thead><tbody>{rows or '<tr><td colspan="5" class="empty">Belum ada transaksi.</td></tr>'}</tbody></table></div></div>
<div class="notice"><b>Catatan tahap awal:</b> laporan laba di sini adalah laba kotor transaksi. Belum memperhitungkan biaya operasional, pajak, gaji, cicilan, atau jurnal akuntansi lengkap.</div>"""
            return self.send(page(layout("Dashboard","Ringkasan operasional cabang utama.",content), user=user))
        if p == "/products":
            with db() as c:
                products = c.execute("""SELECT p.*, COUNT(s.id) unit_count,
                  SUM(CASE WHEN s.status='Tersedia' THEN 1 ELSE 0 END) available
                  FROM products p LEFT JOIN stock_units s ON s.product_id=p.id
                  GROUP BY p.id ORDER BY p.brand,p.model""").fetchall()
                units = c.execute("""SELECT s.*,p.brand,p.model,p.variant,b.name branch
                  FROM stock_units s JOIN products p ON p.id=s.product_id JOIN branches b ON b.id=s.branch_id
                  ORDER BY s.id DESC LIMIT 100""").fetchall()
            prodrows = "".join(f"<tr><td>{esc(r['sku'] or '-')}</td><td>{esc(r['brand'])} {esc(r['model'])}</td><td>{esc(r['variant'])}</td><td>{r['available'] or 0}</td><td>{r['min_stock']}</td></tr>" for r in products)
            unitrows = "".join(f"<tr><td>{esc(r['brand'])} {esc(r['model'])}</td><td><code>{esc(r['imei1'])}</code></td><td>{money(r['cost_price'])}</td><td>{money(r['selling_price'])}</td><td><span class='pill'>{esc(r['status'])}</span></td><td>{esc(r['branch'])}</td></tr>" for r in units)
            with db() as c: product_options = c.execute("SELECT id,brand,model,variant FROM products ORDER BY brand,model").fetchall()
            options = "".join(f"<option value='{r['id']}'>{esc(r['brand'])} {esc(r['model'])} {esc(r['variant'])}</option>" for r in product_options)
            content = f"""<div class="two">
<div class="panel"><h2>Tambah produk</h2><form method="post" action="/product-add" class="form">
<label>SKU (opsional)<input name="sku" placeholder="IPH15-128"></label><label>Merek<input name="brand" required placeholder="Apple / Samsung"></label>
<label>Model<input name="model" required placeholder="iPhone 15"></label><label>Varian<input name="variant" placeholder="128 GB • Black"></label>
<label>Kondisi<select name="condition"><option>Baru</option><option>Bekas</option></select></label><label>Batas stok minimum<input name="min_stock" type="number" min="0" value="1" required></label>
<button>Tambah produk</button></form></div>
<div class="panel"><h2>Tambah unit berdasarkan IMEI</h2><form method="post" action="/unit-add" class="form">
<label>Produk<select name="product_id" required>{options or '<option value="">Tambahkan produk dulu</option>'}</select></label>
<label>IMEI 1<input name="imei1" required minlength="8" maxlength="20" placeholder="Masukkan IMEI asli"></label>
<label>IMEI 2 (opsional)<input name="imei2" maxlength="20"></label><label>Nomor serial (opsional)<input name="serial_number"></label>
<label>Harga modal (rupiah)<input name="cost_price" type="number" min="0" required></label><label>Harga jual (rupiah)<input name="selling_price" type="number" min="0" required></label>
<button>Catat unit</button></form></div></div>
<div class="panel"><h2>Daftar produk</h2><div class="tablewrap"><table><thead><tr><th>SKU</th><th>Produk</th><th>Varian</th><th>Tersedia</th><th>Min. stok</th></tr></thead><tbody>{prodrows or '<tr><td colspan="5" class="empty">Belum ada produk.</td></tr>'}</tbody></table></div></div>
<div class="panel"><h2>Unit / IMEI terbaru</h2><div class="tablewrap"><table><thead><tr><th>Produk</th><th>IMEI 1</th><th>Modal</th><th>Harga jual</th><th>Status</th><th>Cabang</th></tr></thead><tbody>{unitrows or '<tr><td colspan="6" class="empty">Belum ada unit.</td></tr>'}</tbody></table></div></div>"""
            return self.send(page(layout("Produk & IMEI","Satu unit fisik = satu catatan stok.",content), user=user))
        if p == "/sales":
            with db() as c:
                units = c.execute("""SELECT s.id,s.imei1,s.selling_price,s.cost_price,p.brand,p.model,p.variant
                    FROM stock_units s JOIN products p ON p.id=s.product_id WHERE s.status='Tersedia' ORDER BY p.brand,p.model""").fetchall()
            options = "".join(f"<option value='{r['id']}'>{esc(r['brand'])} {esc(r['model'])} {esc(r['variant'])} • IMEI {esc(r['imei1'])} • {money(r['selling_price'])}</option>" for r in units)
            content = f"""<div class="two"><div class="panel"><h2>Buat transaksi penjualan</h2>
<form method="post" action="/sale-add" class="form"><label>Pilih unit / IMEI<select name="unit_id" required>{options or '<option value="">Tidak ada stok tersedia</option>'}</select></label>
<label>Nama pelanggan (opsional)<input name="customer_name" placeholder="Nama pelanggan"></label>
<label>Metode pembayaran<select name="payment_method"><option>Tunai</option><option>Transfer</option><option>QRIS</option><option>Kartu debit/kredit</option></select></label>
<p class="muted small">Tahap ini mencatat satu unit per transaksi. Harga jual mengikuti harga yang tersimpan pada unit.</p><button>Konfirmasi penjualan</button></form></div>
<div class="panel"><h2>Aturan transaksi</h2><ul class="checks"><li>IMEI hanya dapat terjual satu kali.</li><li>Stok berubah menjadi Terjual setelah transaksi berhasil.</li><li>Nomor struk dibuat otomatis.</li><li>Transaksi dasar ini belum mencakup diskon, retur, atau cicilan.</li></ul></div></div>"""
            return self.send(page(layout("Kasir","Pilih unit fisik yang tersedia dan catat pembayaran.",content), user=user))
        if p == "/reports":
            with db() as c:
                rows = c.execute("""SELECT s.*,b.name branch FROM sales s JOIN branches b ON b.id=s.branch_id ORDER BY s.id DESC""").fetchall()
                count = c.execute("SELECT COUNT(*) n FROM sales").fetchone()["n"]
                total = c.execute("SELECT COALESCE(SUM(total),0) n FROM sales").fetchone()["n"]
                gross = c.execute("SELECT COALESCE(SUM(total-total_cost),0) n FROM sales").fetchone()["n"]
            tr = "".join(f"<tr><td>{esc(r['receipt_no'])}</td><td>{esc(r['created_at'])}</td><td>{esc(r['customer_name'] or '-')}</td><td>{esc(r['payment_method'])}</td><td>{money(r['total'])}</td><td>{money(r['total']-r['total_cost'])}</td></tr>" for r in rows)
            content = f"""<div class="cards"><div class="metric"><span>Total transaksi</span><strong>{count}</strong></div><div class="metric"><span>Total omzet</span><strong>{money(total)}</strong></div><div class="metric"><span>Total laba kotor</span><strong>{money(gross)}</strong></div></div>
<div class="panel"><div class="panel-title"><h2>Riwayat penjualan</h2><a class="button secondary" href="/export-sales">Ekspor CSV</a></div><div class="tablewrap"><table><thead><tr><th>Struk</th><th>Tanggal</th><th>Pelanggan</th><th>Metode</th><th>Total</th><th>Laba kotor</th></tr></thead><tbody>{tr or '<tr><td colspan="6" class="empty">Belum ada penjualan.</td></tr>'}</tbody></table></div></div>"""
            return self.send(page(layout("Laporan","Ringkasan transaksi yang tercatat pada database lokal.",content), user=user))
        if p == "/users":
            if user["role"] != "owner": return self.send(page(layout("Akses ditolak","Menu ini hanya untuk Owner.","<a class='button' href='/'>Kembali</a>"), user=user),403)
            with db() as c: users = c.execute("SELECT username,role,active,created_at FROM users ORDER BY id").fetchall()
            userrows = "".join(f"<tr><td>{esc(u['username'])}</td><td>{'Owner' if u['role']=='owner' else 'Kasir'}</td><td>{'Aktif' if u['active'] else 'Nonaktif'}</td><td>{esc(u['created_at'])}</td></tr>" for u in users)
            content = f"<div class='two'><div class='panel'><h2>Tambah akun Kasir</h2><form method='post' action='/user-add' class='form'><label>Nama pengguna<input name='username' required minlength='4' maxlength='40'></label><label>Kata sandi awal (min. 12 karakter)<input type='password' name='password' required minlength='12'></label><button>Buat akun Kasir</button></form></div><div class='panel'><h2>Hak akses</h2><ul class='checks'><li>Owner: semua fitur dasar dan pengelolaan pengguna.</li><li>Kasir: penjualan dan melihat data.</li><li>Kasir tidak dapat menambah produk, stok, atau impor katalog.</li></ul></div></div><div class='panel'><h2>Daftar pengguna</h2><div class='tablewrap'><table><thead><tr><th>Nama pengguna</th><th>Peran</th><th>Status</th><th>Dibuat</th></tr></thead><tbody>{userrows}</tbody></table></div></div>"
            return self.send(page(layout("Pengguna & akses","Kelola akun yang boleh masuk aplikasi.",content),user=user))
        if p == "/import-export":
            content = """<div class="two"><div class="panel"><h2>Impor produk dari CSV</h2><p class="muted">Simpan file Excel sebagai CSV UTF-8. Kolom wajib: brand, model. Kolom opsional: sku, variant, condition, min_stock.</p>
<form method="post" action="/import-products" enctype="application/x-www-form-urlencoded" class="form"><label>Isi CSV (salin-tempel)<textarea name="csv_text" rows="9" placeholder="sku,brand,model,variant,condition,min_stock&#10;IPH15,Apple,iPhone 15,128 GB Black,Baru,1" required></textarea></label><button>Impor produk</button></form></div>
<div class="panel"><h2>Ekspor data</h2><p class="muted">Unduh data stok unit atau riwayat penjualan dalam format CSV yang dapat dibuka di Excel.</p><div class="actions"><a class="button" href="/export-stock">Ekspor stok CSV</a><a class="button secondary" href="/export-sales">Ekspor penjualan CSV</a></div><div class="notice">Buat salinan cadangan file <code>ziestore.db</code> saat aplikasi berhenti. CSV bukan pengganti backup database penuh.</div></div></div>"""
            return self.send(page(layout("Impor / Ekspor","Mulai dengan input manual atau impor data produk.",content), user=user))
        return self.send(page(layout("Halaman tidak ditemukan","Periksa alamat yang dimasukkan.","<a class='button' href='/'>Kembali ke dashboard</a>"),"Tidak ditemukan"),404)

    def do_POST(self):
        p = urlparse(self.path).path
        d = self.body_data()
        try:
            if p == "/setup":
                if user_count() != 0: return self.redirect("/login")
                username=self.val(d,"username").lower(); password=self.val(d,"password")
                if not re.fullmatch(r"[a-zA-Z0-9_.-]{4,40}",username): raise ValueError("Nama pengguna harus 4–40 karakter dan hanya memakai huruf, angka, titik, _ atau -.")
                if len(password)<12: raise ValueError("Kata sandi minimal 12 karakter.")
                if password != self.val(d,"password2"): raise ValueError("Ulangan kata sandi tidak sama.")
                with db() as c: uid=c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",(username,password_hash(password),"owner")).lastrowid
                token=secrets.token_urlsafe(32); SESSIONS[token]={"user_id":uid,"username":username,"role":"owner","expires":time.time()+28800}
                return self.send("",303,headers={"Location":"/","Set-Cookie":f"ziestore_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"})
            if p == "/login":
                username=self.val(d,"username").lower(); password=self.val(d,"password")
                with db() as c: u=c.execute("SELECT * FROM users WHERE username=? AND active=1",(username,)).fetchone()
                if not u or not password_ok(password,u["password_hash"]): return self.send(login_page("Nama pengguna atau kata sandi salah."),401)
                token=secrets.token_urlsafe(32); SESSIONS[token]={"user_id":u["id"],"username":u["username"],"role":u["role"],"expires":time.time()+28800}
                return self.send("",303,headers={"Location":"/","Set-Cookie":f"ziestore_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"})
            if p == "/logout":
                for part in self.headers.get("Cookie", "").split(";"):
                    k,_,v=part.strip().partition("=")
                    if k=="ziestore_session": SESSIONS.pop(v,None)
                return self.send("",303,headers={"Location":"/login","Set-Cookie":"ziestore_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            user=current_user(self)
            if not user: return self.redirect("/login")
            if user["role"]=="cashier" and p in ("/product-add","/unit-add","/import-products"):
                return self.send(page(layout("Akses ditolak","Akun Kasir tidak dapat mengubah katalog atau stok.","<a class='button' href='/'>Kembali</a>"),user=user),403)
            if p == "/user-add":
                if user["role"]!="owner": return self.send("Akses ditolak",403)
                username=self.val(d,"username").lower(); password=self.val(d,"password")
                if not re.fullmatch(r"[a-zA-Z0-9_.-]{4,40}",username): raise ValueError("Nama pengguna harus 4–40 karakter dan hanya memakai huruf, angka, titik, _ atau -.")
                if len(password)<12: raise ValueError("Kata sandi minimal 12 karakter.")
                with db() as c:
                    c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",(username,password_hash(password),"cashier"))
                    c.execute("INSERT INTO audit_logs(user_id,action,detail) VALUES(?,?,?)",(user["user_id"],"USER_ADD",username))
                return self.redirect("/users")
            if p == "/product-add":
                brand, model = self.val(d,"brand"), self.val(d,"model")
                if not brand or not model: raise ValueError("Merek dan model wajib diisi.")
                sku = self.val(d,"sku") or None
                with db() as c:
                    c.execute("INSERT INTO products(sku,brand,model,variant,condition,min_stock) VALUES(?,?,?,?,?,?)",
                      (sku,brand,model,self.val(d,"variant"),self.val(d,"condition","Baru"),max(0,int(self.val(d,"min_stock","1")))))
                    c.execute("INSERT INTO audit_logs(user_id,action,detail) VALUES(?,?,?)",(user["user_id"],"PRODUCT_ADD",f"{brand} {model}"))
                return self.redirect("/products")
            if p == "/unit-add":
                imei = self.val(d,"imei1")
                if not imei.isdigit() or not 8 <= len(imei) <= 20: raise ValueError("IMEI 1 harus berisi 8–20 digit angka.")
                imei2 = self.val(d,"imei2") or None
                if imei2 and (not imei2.isdigit() or not 8 <= len(imei2) <= 20): raise ValueError("IMEI 2 harus berisi 8–20 digit angka.")
                cost, selling = int(self.val(d,"cost_price")), int(self.val(d,"selling_price"))
                if cost < 0 or selling < 0: raise ValueError("Harga tidak boleh negatif.")
                with db() as c:
                    branch = c.execute("SELECT id FROM branches WHERE code='PUSAT'").fetchone()["id"]
                    c.execute("""INSERT INTO stock_units(product_id,branch_id,imei1,imei2,serial_number,cost_price,selling_price)
                        VALUES(?,?,?,?,?,?,?)""",(int(self.val(d,"product_id")),branch,imei,imei2,self.val(d,"serial_number"),cost,selling))
                    c.execute("INSERT INTO audit_logs(user_id,action,detail) VALUES(?,?,?)",(user["user_id"],"STOCK_ADD",f"IMEI {imei}"))
                return self.redirect("/products")
            if p == "/sale-add":
                unit_id = int(self.val(d,"unit_id"))
                with db() as c:
                    unit = c.execute("SELECT * FROM stock_units WHERE id=? AND status='Tersedia'",(unit_id,)).fetchone()
                    if not unit: raise ValueError("Unit tidak tersedia atau sudah terjual.")
                    branch = c.execute("SELECT id FROM branches WHERE code='PUSAT'").fetchone()["id"]
                    receipt = "ZIE-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2).upper()
                    cur = c.execute("""INSERT INTO sales(receipt_no,branch_id,customer_name,payment_method,total,total_cost)
                        VALUES(?,?,?,?,?,?)""",(receipt,branch,self.val(d,"customer_name"),self.val(d,"payment_method","Tunai"),unit["selling_price"],unit["cost_price"]))
                    sale_id = cur.lastrowid
                    c.execute("INSERT INTO sale_items(sale_id,stock_unit_id,imei_snapshot,item_price,cost_snapshot) VALUES(?,?,?,?,?)",
                      (sale_id,unit["id"],unit["imei1"],unit["selling_price"],unit["cost_price"]))
                    c.execute("UPDATE stock_units SET status='Terjual' WHERE id=? AND status='Tersedia'",(unit_id,))
                    c.execute("INSERT INTO audit_logs(user_id,action,detail) VALUES(?,?,?)",(user["user_id"],"SALE",receipt))
                return self.redirect("/reports")
            if p == "/import-products":
                raw = self.val(d,"csv_text")
                reader = csv.DictReader(io.StringIO(raw))
                if not reader.fieldnames or "brand" not in reader.fieldnames or "model" not in reader.fieldnames:
                    raise ValueError("CSV wajib memiliki kolom brand dan model.")
                added = 0
                with db() as c:
                    for row in reader:
                        brand, model = (row.get("brand") or "").strip(), (row.get("model") or "").strip()
                        if not brand or not model: continue
                        sku = (row.get("sku") or "").strip() or None
                        c.execute("""INSERT OR IGNORE INTO products(sku,brand,model,variant,condition,min_stock)
                          VALUES(?,?,?,?,?,?)""",(sku,brand,model,(row.get("variant") or "").strip(),(row.get("condition") or "Baru").strip(),max(0,int(row.get("min_stock") or 1))))
                        added += c.execute("SELECT changes()").fetchone()[0]
                    c.execute("INSERT INTO audit_logs(user_id,action,detail) VALUES(?,?,?)",(user["user_id"],"PRODUCT_IMPORT",f"{added} produk"))
                return self.send(page(layout("Impor selesai",f"{added} produk baru ditambahkan. SKU duplikat dilewati.","<a class='button' href='/products'>Buka produk</a>"),user=user))
        except (ValueError, sqlite3.IntegrityError, KeyError) as e:
            msg = esc(str(e))
            return self.send(page(layout("Tidak dapat menyimpan data","Periksa pesan berikut dan koreksi datanya.",f"<div class='notice error'>{msg}</div><a class='button' href='javascript:history.back()'>Kembali</a>"), user=user if "user" in locals() else None, logged_in=("user" in locals())),400)
        return self.send("Tidak ditemukan",404)

    def do_GET_export(self, kind):
        pass

# Export endpoints are kept separate for straightforward CSV downloads.
_original_do_get = Handler.do_GET
def do_get_with_exports(self):
    p = urlparse(self.path).path
    if p in ("/export-stock","/export-sales"):
        if not current_user(self):
            return self.redirect("/login")
        out = io.StringIO()
        with db() as c:
            if p == "/export-stock":
                rows = c.execute("""SELECT p.sku,p.brand,p.model,p.variant,s.imei1,s.imei2,s.serial_number,
                    s.cost_price,s.selling_price,s.status,b.code branch_code,b.name branch
                    FROM stock_units s JOIN products p ON p.id=s.product_id JOIN branches b ON b.id=s.branch_id
                    ORDER BY s.id""").fetchall()
                headers = ["sku","brand","model","variant","imei1","imei2","serial_number","cost_price","selling_price","status","branch_code","branch"]
                filename = "ziestore-stok.csv"
            else:
                rows = c.execute("""SELECT receipt_no,created_at,customer_name,payment_method,total,total_cost,
                    total-total_cost AS gross_profit FROM sales ORDER BY id""").fetchall()
                headers = ["receipt_no","created_at","customer_name","payment_method","total","total_cost","gross_profit"]
                filename = "ziestore-penjualan.csv"
            w = csv.writer(out); w.writerow(headers)
            for r in rows: w.writerow([r[h] for h in headers])
        data = ("\ufeff"+out.getvalue()).encode("utf-8")
        return self.send(data, content_type="text/csv; charset=utf-8",
                         headers={"Content-Disposition":f'attachment; filename="{filename}"'})
    return _original_do_get(self)
Handler.do_GET = do_get_with_exports

if __name__ == "__main__":
    init_db()
    print(f"ZIESTORE berjalan di http://{HOST}:{PORT}")
    print("Hentikan dengan Ctrl+C. Backup: salin zi​​estore.db setelah server berhenti.".replace("\u200b",""))
    ThreadingHTTPServer((HOST,PORT), Handler).serve_forever()
