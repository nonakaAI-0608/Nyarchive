# -*- coding: utf-8 -*-
"""
Nyarchive - pawchive.pw 画像保存ツール

アップロード者（作者）ページのURLを貼って「保存」を押すと、
  保存先 / 作者名 / yyyymmddhhmmss_作品名 / 1.png 2.png ... + 添付ファイル(psd/動画など)
の形で全投稿を保存します。単一投稿のURLにも対応。
未アーカイブ（プレビューのみ）の作品は 1_preview.jpg ... として縮小画像を保存し、
後で本体画像を取得できたらプレビューを削除します。
"""
import html
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import parse_qs, urljoin, urlparse

import requests

APP_TITLE = "Nyarchive"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "nyarchive_config.json")
HISTORY_PATH = os.path.join(BASE_DIR, "nyarchive_history.json")
PAGE_SIZE = 50
# サイトの要請: 1秒1リクエスト以下・識別可能なUser-Agent（ブラウザ偽装はブロック対象）
REQUEST_INTERVAL = 1.1  # 秒
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
HEADERS = {
    "User-Agent": "Nyarchive/1.0 (personal image archiver; python-requests)",
}

USER_URL_RE = re.compile(r"^/([^/]+)/user/([^/]+)/?$")
POST_URL_RE = re.compile(r"^/([^/]+)/user/([^/]+)/post/([^/]+)/?$")


class StopRequested(Exception):
    pass


# ---------------------------------------------------------------- utilities
def sanitize(name, max_len=120):
    """Windowsで使えない文字を置換し、末尾のドット/空白を除去する。"""
    name = html.unescape(name or "").strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name)
    name = name[:max_len].rstrip(" .")
    if not name:
        name = "untitled"
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", name):
        name = "_" + name
    return name


def published_stamp(text):
    """'2026-09-23 05:23:46' -> '20260923052346'"""
    digits = re.sub(r"\D", "", text or "")
    return digits[:14] if len(digits) >= 14 else (digits or "00000000000000")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# ---------------------------------------------------------------- scraping
_rate_lock = threading.Lock()
_rate_last = [0.0]


def _wait_rate():
    """全スレッド共通で REQUEST_INTERVAL 秒に1回までに抑える。"""
    with _rate_lock:
        wait = REQUEST_INTERVAL - (time.time() - _rate_last[0])
        if wait > 0:
            time.sleep(wait)
        _rate_last[0] = time.time()


def post_id(post_url):
    return post_url.rstrip("/").rsplit("/", 1)[1]


class Scraper:
    def __init__(self, log, stop_event):
        self.log = log
        self.stop_event = stop_event
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def check_stop(self):
        if self.stop_event.is_set():
            raise StopRequested()

    def get(self, url, stream=False, retries=3, headers=None):
        for attempt in range(1, retries + 1):
            self.check_stop()
            _wait_rate()
            try:
                r = self.session.get(url, timeout=(20, 60), stream=stream,
                                     headers=headers)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except (requests.RequestException,) as e:
                if attempt == retries:
                    raise
                self.log(f"  再試行 {attempt}/{retries - 1}: {e}")
                time.sleep(3 * attempt)

    def list_posts(self, origin, service, user_id):
        """作者ページを全ページ巡回し、(作者名, [投稿URL...]) を返す。"""
        user_path = f"/{service}/user/{user_id}"
        artist = None
        posts, seen = [], set()
        offset = 0
        total = None
        while True:
            url = f"{origin}{user_path}" + (f"?o={offset}" if offset else "")
            text = self.get(url).text
            if artist is None:
                m = re.search(r'<meta name="artist_name" content="([^"]*)"', text)
                artist = html.unescape(m.group(1)) if m else user_id
            if total is None:
                m = re.search(r"Showing\s+\d+\s*-\s*\d+\s+of\s+(\d+)", text)
                total = int(m.group(1)) if m else None
            found = 0
            for pid in re.findall(re.escape(user_path) + r"/post/([^\"/?#]+)\"", text):
                if pid not in seen:
                    seen.add(pid)
                    posts.append(f"{origin}{user_path}/post/{pid}")
                    found += 1
            self.log(f"一覧取得: {len(posts)}" + (f" / {total}" if total else "") + " 件")
            if found == 0 or (total is not None and len(posts) >= total):
                break
            offset += PAGE_SIZE
        return artist, posts

    def parse_post(self, url):
        text = self.get(url).text
        m = re.search(r'<h1 class="post__title">\s*<span>(.*?)</span>', text, re.S)
        title = strip_tags(m.group(1)) if m else ""
        m = re.search(r'<meta name="published" content="([^"]*)"', text)
        published = m.group(1) if m else ""
        if not published:
            m = re.search(r"Published:</div>\s*([0-9:\- ]+)", text)
            published = m.group(1).strip() if m else ""
        m = re.search(r'<meta name="artist_name" content="([^"]*)"', text)
        artist = html.unescape(m.group(1)) if m else None
        if not artist:
            m = re.search(r'"author":\s*\{[^}]*?"name":\s*("(?:[^"\\]|\\.)*")', text)
            if m:
                try:
                    artist = json.loads(m.group(1))
                except ValueError:
                    pass

        # 本体画像（Files欄）
        images, seen = [], set()
        for href in re.findall(r'class="fileThumb"\s+href="([^"]+)"', text):
            href = urljoin(url, html.unescape(href))
            ext = image_ext(href)
            if ext and data_key(href) not in seen:
                seen.add(data_key(href))
                images.append((href, ext))

        # プレビュー（未アーカイブ作品の縮小画像）
        previews = []
        for src in re.findall(
                r'class="post__thumbnail">\s*<figure>\s*<img\s+(?:data-)?src="([^"]+)"', text):
            src = urljoin(url, html.unescape(src))
            ext = image_ext(src)
            if ext and src not in [u for u, _ in previews]:
                previews.append((src, ext))

        # 添付ファイル（Downloads欄: psd / zip / 動画 など）
        attachments = []
        for a in re.finditer(r'<a\s+class="post__attachment-link"([^>]*)>', text):
            attrs = a.group(1)
            m = re.search(r'href="([^"]+)"', attrs)
            if not m:
                continue
            href = urljoin(url, html.unescape(m.group(1)))
            if data_key(href) in seen:
                continue
            seen.add(data_key(href))
            m = re.search(r'download="([^"]*)"', attrs)
            name = html.unescape(m.group(1)) if m else ""
            if not name:
                name = parse_qs(urlparse(href).query).get("f", [""])[0] \
                    or os.path.basename(urlparse(href).path)
            attachments.append((href, name))
        missing_attachments = len(re.findall(r"post__attachment-link--missing", text))

        # サイト側で本体が未アーカイブ（プレビューのみ）の投稿
        not_archived = ("haven't archived this post yet" in text
                        or "post__previews-note" in text)
        return {"title": title or "untitled", "published": published,
                "artist": artist, "images": images, "previews": previews,
                "attachments": attachments, "missing_attachments": missing_attachments,
                "not_archived": not_archived}


    def check_updates(self, url, known_posts, unarchived, check_archived=True):
        """履歴の作者に (新しい投稿ID, アーカイブ化された投稿ID) があるか調べる。
        check_archived=False なら新着だけ確認し、アーカイブ化は None を返す。"""
        p = urlparse(url)
        m = USER_URL_RE.match(p.path.rstrip("/"))
        service, user_id = m.groups()
        _, post_urls = self.list_posts(f"{p.scheme}://{p.netloc}", service, user_id)
        current = [post_id(u) for u in post_urls]
        known = set(known_posts)
        new = [i for i in current if i not in known]
        if not check_archived:
            return new, None
        archived = []
        targets = [u for u in post_urls if post_id(u) in set(unarchived)]
        for n, u in enumerate(targets, 1):
            self.check_stop()
            try:
                if not self.parse_post(u)["not_archived"]:
                    archived.append(post_id(u))
            except StopRequested:
                raise
            except Exception as e:
                self.log(f"    確認に失敗: {u} ({e})")
        return new, archived


def image_ext(href):
    p = urlparse(href)
    name = parse_qs(p.query).get("f", [os.path.basename(p.path)])[0]
    ext = os.path.splitext(name)[1].lower()
    if ext not in IMAGE_EXTS:
        ext = os.path.splitext(p.path)[1].lower()
    return ext if ext in IMAGE_EXTS else None


def data_key(href):
    """同じファイルかどうかの判定用（サーバー名と ?f= を除いたパス末尾のハッシュ名）"""
    return os.path.basename(urlparse(href).path)


PREVIEW_RE = re.compile(r"^\d+_preview\.[A-Za-z0-9]+$")


def download(sc, url, out, stop_event):
    """1ファイル保存。'saved' / 'skipped' を返し、失敗時は例外。"""
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return "skipped"
    tmp = out + ".part"
    try:
        with sc.get(url, stream=True) as r:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if stop_event.is_set():
                        break
                    f.write(chunk)
        if stop_event.is_set():
            raise StopRequested()
        os.replace(tmp, out)
        return "saved"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------- worker
def author_key(url):
    """作者ページURLなら正規化した 'https://host/service/user/id' を返す（作品URLなどは None）。"""
    url = (url or "").strip()
    p = urlparse(url if "://" in url else "https://" + url)
    m = USER_URL_RE.match(p.path.rstrip("/") or "/")
    if not m or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}/{m.group(1)}/user/{m.group(2)}"


class History:
    """作者単位のダウンロード履歴（nyarchive_history.json）"""

    def __init__(self, path=HISTORY_PATH):
        self.path = path
        self.items = []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.items = [x for x in data.get("authors", []) if x.get("url")]
        except FileNotFoundError:
            pass
        except Exception:
            # 壊れている場合は退避して新規作成（履歴を黙って消さない）
            try:
                os.replace(path, path + ".broken")
            except OSError:
                pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"authors": self.items}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def find(self, url):
        key = author_key(url)
        return next((x for x in self.items if x["url"] == key), None) if key else None

    def record(self, url, artist, include_cover, status, result=None):
        key = author_key(url)
        if not key:
            return
        item = self.find(key) or {"url": key}
        if item in self.items:
            self.items.remove(item)
        service = key.split("/")[3]
        item.update(artist=artist or item.get("artist") or key.rsplit("/", 1)[1],
                    service=service, include_cover=bool(include_cover),
                    last_saved=time.strftime("%Y-%m-%d %H:%M"), status=status)
        if result is not None:  # 最後まで保存できたときだけ、更新確認の基準を更新
            item["posts"] = result["posts"]
            item["unarchived"] = result["unarchived"]
            item.pop("pending", None)
        self.items.insert(0, item)  # 新しい順
        self.save()

    def set_pending(self, url, new, archived):
        item = self.find(url)
        if not item:
            return
        if archived is None:  # 新着のみの確認では、前回見つけたアーカイブ化はそのまま残す
            archived = (item.get("pending") or {}).get("archived", [])
        if new or archived:
            item["pending"] = {"new": new, "archived": archived}
        else:
            item.pop("pending", None)
        item["checked"] = time.strftime("%Y-%m-%d %H:%M")
        self.save()

    def remove(self, url):
        item = self.find(url)
        if item:
            self.items.remove(item)
            self.save()

    @staticmethod
    def update_text(x):
        pend = x.get("pending") or {}
        parts = []
        if pend.get("new"):
            parts.append(f"新着{len(pend['new'])}")
        if pend.get("archived"):
            parts.append(f"アーカイブ化{len(pend['archived'])}")
        return " / ".join(parts)

    @staticmethod
    def label(x):
        cover = "カバーあり" if x.get("include_cover", True) else "カバーなし"
        upd = History.update_text(x)
        return (("★" + upd + "  " if upd else "")
                + f"{x.get('artist', '?')}  [{x.get('service', '')}]  "
                f"{x.get('last_saved', '')} {x.get('status', '')}  ({cover})")


def run_download(url, dest, include_cover, log, progress, stop_event, on_author=None):
    url = url.strip()
    p = urlparse(url)
    if not p.scheme:
        p = urlparse("https://" + url)
    origin = f"{p.scheme}://{p.netloc}"
    path = p.path.rstrip("/") or "/"
    sc = Scraper(log, stop_event)

    m_post = POST_URL_RE.match(path)
    m_user = USER_URL_RE.match(path)
    if m_post:
        service, user_id, pid = m_post.groups()
        post_urls = [f"{origin}/{service}/user/{user_id}/post/{pid}"]
        artist = None
    elif m_user:
        service, user_id = m_user.groups()
        log(f"作者ページを読み込み中: {origin}/{service}/user/{user_id}")
        artist, post_urls = sc.list_posts(origin, service, user_id)
        if on_author:
            on_author(artist)  # 作者ページの場合だけ履歴に残す
    else:
        raise ValueError("URLの形式が違います。\n例: https://pawchive.pw/fanbox/user/26213428")

    if not post_urls:
        log("投稿が見つかりませんでした。")
        return
    log(f"投稿数: {len(post_urls)}")

    st = {"saved": 0, "skipped": 0, "failed": 0, "preview": 0, "attach": 0, "removed": 0}
    done_posts, unarchived = [], []  # 履歴用: 処理できた投稿 / 未アーカイブの投稿
    for i, purl in enumerate(post_urls, 1):
        sc.check_stop()
        progress(i - 1, len(post_urls))
        head = f"[{i}/{len(post_urls)}]"
        try:
            info = sc.parse_post(purl)
        except StopRequested:
            raise
        except Exception as e:
            st["failed"] += 1
            log(f"{head} 投稿の取得に失敗: {purl} ({e})")
            continue  # 取得できなかった投稿は記録しない（次回の更新確認で新着として出る）
        done_posts.append(post_id(purl))
        if info["not_archived"]:
            unarchived.append(post_id(purl))
        if artist is None:
            artist = info["artist"] or user_id
        artist_dir = os.path.join(dest, sanitize(artist))
        folder = f"{published_stamp(info['published'])}_{sanitize(info['title'], 100)}"
        post_dir = os.path.join(artist_dir, folder)

        is_preview = False
        images = info["images"]
        if not images and info["previews"]:
            images, is_preview = info["previews"], True
        if not include_cover and len(images) > 1:
            images = images[1:]  # 先頭はカバー画像
        attachments = info["attachments"]

        if not images and not attachments:
            reason = "サイト未アーカイブ・プレビューなし" if info["not_archived"] else "画像なし"
            log(f"{head} {folder} : {reason}")
            continue

        parts = []
        if images:
            parts.append(f"プレビュー{len(images)}枚" if is_preview else f"画像{len(images)}枚")
        if attachments:
            parts.append(f"添付{len(attachments)}件")
        note = " ※未アーカイブのため縮小画像を保存" if is_preview else ""
        log(f"{head} {folder} ({' / '.join(parts)}){note}")
        os.makedirs(post_dir, exist_ok=True)

        post_failed = False
        # 画像（連番）
        for n, (img_url, ext) in enumerate(images, 1):
            sc.check_stop()
            name = f"{n}_preview{ext}" if is_preview else f"{n}{ext}"
            try:
                r = download(sc, img_url, os.path.join(post_dir, name), stop_event)
                st[r] += 1
                if r == "saved" and is_preview:
                    st["preview"] += 1
            except StopRequested:
                raise
            except Exception as e:
                st["failed"] += 1
                post_failed = True
                log(f"    {name} の保存に失敗: {e}")

        # 添付ファイル（元のファイル名で保存）
        used = {f"{n}{e}".lower() for n, (_, e) in enumerate(images, 1)}
        for att_url, att_name in attachments:
            sc.check_stop()
            name = sanitize(att_name, 150)
            base, ext = os.path.splitext(name)
            k = 2
            while name.lower() in used:
                name = f"{base} ({k}){ext}"
                k += 1
            used.add(name.lower())
            try:
                r = download(sc, att_url, os.path.join(post_dir, name), stop_event)
                st[r] += 1
                if r == "saved":
                    st["attach"] += 1
            except StopRequested:
                raise
            except Exception as e:
                st["failed"] += 1
                post_failed = True
                log(f"    添付 {name} の保存に失敗: {e}")

        if info["missing_attachments"] and not attachments:
            log(f"    添付{info['missing_attachments']}件はサイト未アーカイブのため保存できません")

        # 本体画像がすべて揃ったら、以前保存したプレビューを削除
        if images and not is_preview and not post_failed:
            for fn in os.listdir(post_dir):
                if PREVIEW_RE.match(fn):
                    os.remove(os.path.join(post_dir, fn))
                    st["removed"] += 1
                    log(f"    本体画像を取得できたため {fn} を削除")

        if os.path.isdir(post_dir) and not os.listdir(post_dir):
            os.rmdir(post_dir)  # 1つも保存できなかった空フォルダは残さない
    progress(len(post_urls), len(post_urls))
    log(f"完了: 保存 {st['saved']}（うちプレビュー {st['preview']} / 添付 {st['attach']}）"
        f" / 既存スキップ {st['skipped']} / 失敗 {st['failed']}"
        + (f" / プレビュー削除 {st['removed']}" if st["removed"] else ""))
    log(f"保存先: {os.path.join(dest, sanitize(artist or user_id))}")
    return {"posts": done_posts, "unarchived": unarchived}


# ---------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("800x500")
        root.minsize(560, 360)
        self.cfg = self.load_config()
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.check_stop_event = threading.Event()
        self.checker = None
        self.history = History()
        self.current = None  # 実行中の (url, include_cover)
        self.author_seen = False

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="履歴").grid(row=0, column=0, sticky="w")
        self.hist_var = tk.StringVar()
        self.hist_box = ttk.Combobox(frm, textvariable=self.hist_var, state="readonly")
        self.hist_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.hist_box.bind("<<ComboboxSelected>>", self.on_history_select)
        ttk.Button(frm, text="履歴から削除", command=self.remove_history).grid(row=0, column=2)

        ttk.Label(frm, text="URL").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(frm, textvariable=self.url_var)
        url_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(frm, text="貼り付け", command=self.paste).grid(row=1, column=2, pady=(6, 0))

        ttk.Label(frm, text="保存先").grid(row=2, column=0, sticky="w", pady=6)
        self.dest_var = tk.StringVar(value=self.cfg.get("dest", ""))
        ttk.Entry(frm, textvariable=self.dest_var).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="参照...", command=self.browse).grid(row=2, column=2)

        opt = ttk.Frame(frm)
        opt.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.cover_var = tk.BooleanVar(value=self.cfg.get("include_cover", True))
        ttk.Checkbutton(opt, text="カバー画像も保存する（1番目の画像）",
                        variable=self.cover_var).pack(side="left")
        self.autocheck_var = tk.BooleanVar(value=self.cfg.get("check_on_start", True))
        ttk.Checkbutton(opt, text="起動時に新着を確認する",
                        variable=self.autocheck_var,
                        command=self.save_config).pack(side="left", padx=(12, 0))
        self.stop_btn = ttk.Button(opt, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.save_btn = ttk.Button(opt, text="保存", command=self.start)
        self.save_btn.pack(side="right", padx=6)
        self.check_btn = ttk.Button(opt, text="更新確認",
                                    command=lambda: self.start_check(full=True))
        self.check_btn.pack(side="right")

        self.pb = ttk.Progressbar(frm, mode="determinate")
        self.pb.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        self.status = ttk.Label(frm, text="")
        self.status.grid(row=5, column=0, columnspan=3, sticky="w")

        logf = ttk.Frame(frm)
        logf.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        frm.rowconfigure(6, weight=1)
        self.log_text = tk.Text(logf, height=12, wrap="none", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        url_entry.bind("<Return>", lambda e: self.start())
        url_entry.focus_set()
        self.refresh_history()
        if self.autocheck_var.get():
            self.root.after(500, lambda: self.start_check(full=False))  # 起動時は新着のみ
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll)

    # config
    def load_config(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config(self):
        self.cfg.pop("use_kemono", None)
        self.cfg.update(dest=self.dest_var.get(), include_cover=self.cover_var.get(),
                        check_on_start=self.autocheck_var.get())
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # history
    def refresh_history(self):
        self.hist_box.configure(values=[History.label(x) for x in self.history.items])
        self.hist_var.set("")

    def on_history_select(self, _e=None):
        i = self.hist_box.current()
        if 0 <= i < len(self.history.items):
            x = self.history.items[i]
            self.url_var.set(x["url"])
            self.cover_var.set(x.get("include_cover", True))

    def remove_history(self):
        i = self.hist_box.current()
        if not (0 <= i < len(self.history.items)):
            messagebox.showinfo(APP_TITLE, "削除する履歴をプルダウンから選んでください。")
            return
        x = self.history.items[i]
        if messagebox.askyesno(APP_TITLE, f"履歴から削除しますか？\n{x.get('artist')}\n{x['url']}\n"
                               "（保存済みのファイルは削除されません）"):
            self.history.remove(x["url"])
            self.refresh_history()

    # update check
    def start_check(self, full=True):
        """full=True: 新着 + 未アーカイブ作品のアーカイブ化 / False: 新着のみ"""
        if self.checker and self.checker.is_alive():
            return
        targets = [(x["url"], x.get("artist", "?"), x.get("posts"), x.get("unarchived", []))
                   for x in self.history.items]
        if not targets:
            return
        self.check_stop_event.clear()
        self.check_btn.configure(state="disabled")
        self.checker = threading.Thread(target=self._check_work, args=(targets, full),
                                        daemon=True)
        self.checker.start()

    def _check_work(self, targets, full):
        put = self.q.put
        kind = "新着＋アーカイブ化" if full else "新着のみ"
        put(("log", f"更新確認を開始（{len(targets)}人・{kind}）"))
        sc = Scraper(lambda m: put(("log", m)), self.check_stop_event)
        found = []
        try:
            for n, (url, artist, posts, unarch) in enumerate(targets, 1):
                put(("checkstatus", f"更新確認中 ({n}/{len(targets)}) {artist}"))
                if posts is None:
                    put(("log", f"  {artist}: 基準データがないため確認できません"
                                "（一度最後まで保存してください）"))
                    continue
                try:
                    new, archived = sc.check_updates(url, posts, unarch, check_archived=full)
                except StopRequested:
                    raise
                except Exception as e:
                    put(("log", f"  {artist}: 確認に失敗 ({e})"))
                    continue
                put(("pending", (url, new, archived)))
                text = History.update_text({"pending": {"new": new, "archived": archived}})
                put(("log", f"  {artist}: {text or '更新なし'}"))
                if text:
                    found.append(f"{artist}: {text}")
        except StopRequested:
            put(("log", "更新確認を中止しました。"))
        put(("checkdone", found))

    # actions
    def paste(self):
        try:
            self.url_var.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.dest_var.get() or BASE_DIR)
        if d:
            self.dest_var.set(os.path.normpath(d))

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        url, dest = self.url_var.get().strip(), self.dest_var.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "URLを入力してください。")
            return
        if not dest:
            messagebox.showwarning(APP_TITLE, "保存先フォルダを指定してください。")
            return
        prev = self.history.find(url)
        if prev and prev.get("include_cover", True) != self.cover_var.get():
            was = "保存する" if prev.get("include_cover", True) else "保存しない"
            ans = messagebox.askyesnocancel(
                APP_TITLE,
                f"この作者は前回「カバー画像を{was}」設定で保存しています。\n"
                "設定が違うと連番がずれて画像が重複します。\n\n"
                "前回の設定に合わせて保存しますか？\n"
                "（いいえ = 今の設定のまま保存）")
            if ans is None:
                return
            if ans:
                self.cover_var.set(prev.get("include_cover", True))
        os.makedirs(dest, exist_ok=True)
        self.save_config()
        self.current = (url, self.cover_var.get())
        self.author_seen = False
        self.stop_event.clear()
        self.save_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.pb.configure(value=0)
        self.worker = threading.Thread(
            target=self._work, args=(url, dest, self.cover_var.get()),
            daemon=True)
        self.worker.start()

    def _work(self, url, dest, include_cover):
        try:
            result = run_download(url, dest, include_cover,
                                  lambda m: self.q.put(("log", m)),
                                  lambda a, b: self.q.put(("progress", (a, b))),
                                  self.stop_event,
                                  on_author=lambda a: self.q.put(("author", (a, "保存中"))))
            self.q.put(("author", (None, "完了", result)))
        except StopRequested:
            self.q.put(("log", "停止しました。"))
            self.q.put(("author", (None, "中断")))
        except Exception as e:
            self.q.put(("author", (None, "エラー")))
            self.q.put(("log", f"エラー: {e}"))
            self.q.put(("error", str(e)))
        finally:
            self.q.put(("done", None))

    def stop(self):
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.status.configure(text="停止中...")

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", val + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "progress":
                    a, b = val
                    self.pb.configure(maximum=max(b, 1), value=a)
                    self.status.configure(text=f"{a} / {b} 投稿")
                elif kind == "author":
                    artist, status = val[0], val[1]
                    result = val[2] if len(val) > 2 else None
                    url, cover = self.current or (None, None)
                    # 作者ページの一覧取得に成功した実行だけを履歴に残す
                    if artist:
                        self.author_seen = True
                    if url and self.author_seen:
                        self.history.record(url, artist, cover, status, result)
                        self.refresh_history()
                elif kind == "pending":
                    self.history.set_pending(*val)
                    self.refresh_history()
                elif kind == "checkstatus":
                    if not (self.worker and self.worker.is_alive()):
                        self.status.configure(text=val)
                elif kind == "checkdone":
                    self.check_btn.configure(state="normal")
                    if not (self.worker and self.worker.is_alive()):
                        self.status.configure(text="")
                    if val:
                        messagebox.showinfo(
                            APP_TITLE, "更新がある作者があります。\n\n" + "\n".join(val)
                            + "\n\n履歴（★印）から選んで保存してください。")
                elif kind == "error":
                    messagebox.showerror(APP_TITLE, val)
                elif kind == "done":
                    self.save_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_close(self):
        self.save_config()
        self.stop_event.set()
        self.check_stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
