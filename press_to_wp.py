#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
press_to_wp.py
Mỗi ngày một lần: đọc hộp mail riêng, lấy các thông cáo báo chí,
upload ảnh inline lên Media Library của WordPress, dọn nhẹ phần rác
của mail forward (tùy chọn, bằng Claude), rồi đăng bài.

Trạng thái "đã xử lý" không nằm trong script, mà nằm ngay trong hộp mail:
mail nào làm xong thì đánh dấu đã đọc (và dời sang folder Done nếu khai báo),
nên mỗi lần chạy chỉ nhìn thấy mail mới. Máy ảo của GitHub quên hết là chuyện
bình thường, hộp mail mới là cuốn sổ ghi nhớ.
"""

import os
import re
import sys
import json
import imaplib
from email import message_from_bytes
from email.header import decode_header

import requests

# ---------- Cấu hình lấy từ biến môi trường (GitHub Secrets) ----------
def _env(key, default=""):
    # GitHub luôn truyền biến vào, secret không khai báo sẽ thành chuỗi rỗng.
    # Nên phải tự quy về mặc định khi rỗng, không dùng được mặc định của os.environ.get.
    v = os.environ.get(key, "")
    v = v.strip() if v else ""
    return v if v else default

IMAP_HOST       = os.environ["IMAP_HOST"].strip()
IMAP_USER       = os.environ["IMAP_USER"].strip()
IMAP_PASS       = os.environ["IMAP_PASS"]
IMAP_FOLDER     = _env("IMAP_FOLDER", "INBOX")
PROCESSED_FOLDER = _env("PROCESSED_FOLDER", "")  # ví dụ "Done", để trống thì chỉ đánh dấu đã đọc

# Lọc bằng cú pháp tìm kiếm của Gmail (X-GM-RAW). Mặc định chừa các thẻ Quảng cáo,
# Mạng xã hội, Diễn đàn. Khi dùng nhãn riêng thì đổi thành "label:TenNhan is:unread".
# Trên hộp mail KHÔNG phải Gmail, script tự quay về lọc UNSEEN thường.
GMAIL_RAW_SEARCH = _env("GMAIL_RAW_SEARCH",
                        "is:unread -category:promotions -category:social -category:forums")

WP_URL          = os.environ["WP_URL"].strip().rstrip("/")
WP_USER         = os.environ["WP_USER"].strip()
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]  # không strip vì app password của WP có dấu cách
WP_CATEGORY_ID  = _env("WP_CATEGORY_ID", "")
POST_STATUS     = _env("POST_STATUS", "publish")  # publish | draft

# Ảnh inline/đính kèm nhỏ hơn ngưỡng này gần như chắc là logo, icon, chữ ký,
# nên bỏ đi. Tăng lên nếu logo agency vẫn lọt, giảm xuống nếu lỡ rớt ảnh thật.
MIN_IMAGE_BYTES = int(_env("MIN_IMAGE_BYTES", "20000"))

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = _env("ANTHROPIC_MODEL", "claude-sonnet-4-6")
AI_CLEANUP        = _env("AI_CLEANUP", "true").lower() == "true" and bool(ANTHROPIC_API_KEY)

# Khi bài chỉ có ảnh nằm ngoài (Drive...) mà không nhúng được ảnh nào, hạ về draft
# để Lukas tự bỏ ảnh vào trước khi xuất bản. Đặt "false" nếu vẫn muốn đăng thẳng.
DRAFT_WHEN_IMAGES_EXTERNAL = _env("DRAFT_WHEN_IMAGES_EXTERNAL", "true").lower() == "true"

DRY_RUN = _env("DRY_RUN", "false").lower() == "true"  # bật khi test: không đăng, không đánh dấu

WP_AUTH = (WP_USER, WP_APP_PASSWORD)
EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
}


# ---------- Tiện ích ----------
def log(*a):
    print(*a, flush=True)


def decode_mime(s):
    """Giải mã header dạng =?utf-8?...?= thành chuỗi đọc được."""
    if not s:
        return ""
    out = ""
    for text, enc in decode_header(s):
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def clean_title(subject):
    """Bỏ tiền tố Fwd:/Re:/Fw: lặp lại ở đầu tiêu đề."""
    return re.sub(r"^(\s*(fwd|fw|re)\s*:\s*)+", "", subject, flags=re.I).strip()


def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html or "").strip()


def ensure_ext(filename, ctype):
    if filename and "." in filename:
        return filename
    base = filename or "image"
    return base + EXT_BY_TYPE.get(ctype, ".bin")


# ---------- WordPress ----------
def wp_upload_media(filename, data, ctype):
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/media",
        auth=WP_AUTH,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": ctype,
        },
        data=data,
        timeout=120,
    )
    r.raise_for_status()
    j = r.json()
    return j["id"], j["source_url"]


def wp_create_post(title, content, status, featured_media, category_id):
    payload = {"title": title, "content": content, "status": status}
    if featured_media:
        payload["featured_media"] = featured_media
    if category_id:
        payload["categories"] = [int(category_id)]
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        auth=WP_AUTH,
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("link", "(không lấy được link)")


# ---------- Bóc email ----------
def parse_email(msg):
    """Trả về (html_body, inline_images, attachments)."""
    html_body = None
    text_body = None
    inline_images = {}   # cid -> (filename, data, ctype)
    attachments = []     # (filename, data, ctype)

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        cdisp = str(part.get("Content-Disposition") or "").lower()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        if ctype == "text/html" and "attachment" not in cdisp and html_body is None:
            charset = part.get_content_charset() or "utf-8"
            html_body = payload.decode(charset, errors="replace")
        elif ctype == "text/plain" and "attachment" not in cdisp and text_body is None:
            charset = part.get_content_charset() or "utf-8"
            text_body = payload.decode(charset, errors="replace")
        elif ctype.startswith("image/"):
            fname = ensure_ext(decode_mime(part.get_filename()), ctype)
            cid = part.get("Content-ID")
            if cid:
                inline_images[cid.strip("<>")] = (fname, payload, ctype)
            else:
                attachments.append((fname, payload, ctype))

    if html_body is None and text_body is not None:
        # không có HTML thì gói tạm text vào thẻ p
        safe = text_body.replace("\n", "<br>\n")
        html_body = f"<p>{safe}</p>"

    return html_body, inline_images, attachments


HEADER_LABELS = r"(?:Từ|From|Ngày|Date|Sent|Đã gửi|Subject|Tiêu đề|To|Đến|Gửi|Cc|Bcc)"


def strip_forward_wrapper(html):
    """Cắt phần đầu của mail forward: lời chào, chữ ký người gửi đi, dòng
    'Forwarded message' và mấy dòng Từ/Date/Subject/To trích dẫn ngay sau đó.
    Phần thân thông cáo thật nằm bên dưới nên được giữ lại."""
    if not html:
        return html
    m = re.search(r"-*\s*forwarded message\s*-*", html, flags=re.I)
    if m:
        html = html[m.end():]
    # bóc từng dòng tiêu đề mail trích dẫn ở đầu, tối đa vài dòng liên tiếp
    for _ in range(8):
        new = re.sub(
            r"^\s*(?:<[^>]+>\s*)*" + HEADER_LABELS + r"\s*:.*?(?:<br\s*/?>|</div>|</p>|\n|$)",
            "", html, count=1, flags=re.I | re.S,
        )
        if new == html:
            break
        html = new
    return html.strip()


def remap_inline_images(html, inline_images):
    """Upload từng ảnh inline, thay cid: bằng URL thật. Trả về (html, featured_media_id)."""
    featured = None
    for cid, (fname, data, ctype) in inline_images.items():
        if len(data) < MIN_IMAGE_BYTES:
            # ảnh nhỏ, gần như chắc là logo/chữ ký: gỡ luôn thẻ img ra khỏi bài
            html = re.sub(rf"<img[^>]*cid:{re.escape(cid)}[^>]*>", "", html, flags=re.I)
            local = cid.split("@")[0]
            if local != cid:
                html = re.sub(rf"<img[^>]*cid:{re.escape(local)}[^>]*>", "", html, flags=re.I)
            log(f"   . bỏ ảnh nhỏ (nghi logo/chữ ký): {fname} {len(data)}B")
            continue
        try:
            media_id, url = wp_upload_media(fname, data, ctype)
        except Exception as e:
            log(f"   ! upload ảnh inline {fname} lỗi, bỏ qua: {e}")
            continue
        if featured is None:
            featured = media_id
        # thay cả dạng đầy đủ lẫn dạng rút gọn phần trước @ phòng khi client cắt bớt
        html = html.replace(f"cid:{cid}", url)
        local = cid.split("@")[0]
        if local != cid:
            html = html.replace(f"cid:{local}", url)
    return html, featured


def append_attached_images(html, attachments, featured):
    """Ảnh đính kèm rời (không inline) thì upload và chèn xuống cuối bài."""
    for fname, data, ctype in attachments:
        if len(data) < MIN_IMAGE_BYTES:
            log(f"   . bỏ ảnh đính kèm nhỏ (nghi logo): {fname} {len(data)}B")
            continue
        try:
            media_id, url = wp_upload_media(fname, data, ctype)
        except Exception as e:
            log(f"   ! upload ảnh đính kèm {fname} lỗi, bỏ qua: {e}")
            continue
        if featured is None:
            featured = media_id
        html += f'\n<figure><img src="{url}" alt=""/></figure>'
    return html, featured


# ---------- Xử lý bằng Claude: bóc nội dung + nhận diện ảnh ngoài ----------
def ai_extract(subject, html):
    """Giao thân email cho Claude, nhận về tiêu đề sạch, nội dung sạch và
    danh sách link ảnh ngoài (Drive...). Trả về dict, hoặc None nếu không dùng được."""
    if not AI_CLEANUP:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        instruction = (
            "Bạn nhận phần thân HTML của một email thông cáo báo chí đã được forward. "
            "Nhiệm vụ: bóc ra đúng nội dung thông cáo để đăng web.\n\n"
            "PHẢI loại bỏ những thứ không thuộc nội dung thông cáo:\n"
            "- Lời chào và chữ ký của người forward (vd 'Thank you and best regards', tên người gửi đi).\n"
            "- Dòng 'Forwarded message' và các dòng Từ/From, Date, Subject, To.\n"
            "- Lời chào mở đầu của bên phát thông cáo (vd 'Thân gửi anh chị em', 'Anh/Chị nhà báo thân mến').\n"
            "- Câu mời hỗ trợ truyền thông và lời cảm ơn xã giao (vd 'Rất mong anh chị hỗ trợ', 'Xin chân thành cảm ơn').\n"
            "- Chữ ký, thông tin liên hệ, chức danh, số điện thoại, logo của bên phát thông cáo ở cuối.\n"
            "- Nếu đầu thư có một danh sách vài tiêu đề gợi ý đặt tít, hãy chọn một làm tiêu đề và bỏ danh sách đó khỏi thân.\n\n"
            "TUYỆT ĐỐI không viết lại, tóm tắt hay đổi câu chữ của phần nội dung thông cáo. "
            "Giữ nguyên các thẻ <img> cùng src đang có trong HTML.\n"
            "Nếu trong thư có link tới ảnh ngoài (Google Drive, Dropbox, link tải ảnh...), "
            "hãy bỏ dòng chứa link đó khỏi nội dung và liệt kê link vào external_image_links.\n\n"
            "Trả về DUY NHẤT một JSON, không thêm chữ nào, không bọc trong nháy code:\n"
            '{"title": "...", "content_html": "...", "external_image_links": ["..."]}\n\n'
            f"Tiêu đề email gốc: {subject}\n\nHTML:\n{html}"
        )
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8000,
            messages=[{"role": "user", "content": instruction}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
        data = json.loads(raw)

        content = (data.get("content_html") or "").strip()
        # Van an toàn: nếu Claude làm rỗng nội dung hoặc đánh rơi ảnh thì coi như hỏng
        if len(strip_tags(content)) < 20:
            log("   ! Claude trả về nội dung quá ngắn, dùng bản lọc cứng")
            return None
        if "<img" in html and "<img" not in content and not data.get("external_image_links"):
            log("   ! Claude làm mất ảnh, dùng bản lọc cứng")
            return None
        return {
            "title": (data.get("title") or "").strip(),
            "content_html": content,
            "external_image_links": data.get("external_image_links") or [],
        }
    except Exception as e:
        log(f"   ! Bước Claude lỗi, dùng bản lọc cứng: {e}")
        return None


# ---------- IMAP ----------
def mark_processed(imap, uid):
    if DRY_RUN:
        return
    imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
    if PROCESSED_FOLDER:
        res, _ = imap.uid("COPY", uid, f'"{PROCESSED_FOLDER}"')
        if res == "OK":
            imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            imap.expunge()


def main():
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(IMAP_USER, IMAP_PASS)
    imap.select(f'"{IMAP_FOLDER}"')

    uids = []
    if GMAIL_RAW_SEARCH:
        # Lọc theo cú pháp Gmail: chừa các thẻ không mong muốn (hoặc lọc theo nhãn).
        try:
            typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"{GMAIL_RAW_SEARCH}"')
            if typ != "OK":
                raise imaplib.IMAP4.error(f"X-GM-RAW trả về {typ}")
            uids = data[0].split() if data and data[0] else []
            log(f"Lọc Gmail: {GMAIL_RAW_SEARCH}")
        except Exception as e:
            log(f"Lọc Gmail không dùng được ({e}), quay về lọc mail chưa đọc thường.")
            typ, data = imap.uid("SEARCH", None, "UNSEEN")
            uids = data[0].split() if data and data[0] else []
    else:
        typ, data = imap.uid("SEARCH", None, "UNSEEN")
        uids = data[0].split() if data and data[0] else []

    log(f"Tìm thấy {len(uids)} mail cần xử lý trong '{IMAP_FOLDER}'."
        + (" [DRY_RUN]" if DRY_RUN else ""))

    published = drafted = failed = 0

    for uid in uids:
        try:
            typ, msg_data = imap.uid("FETCH", uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = message_from_bytes(raw)

            title = clean_title(decode_mime(msg.get("Subject")))
            html, inline, attach = parse_email(msg)
            html = strip_forward_wrapper(html or "")

            html, featured = remap_inline_images(html, inline)
            html, featured = append_attached_images(html, attach, featured)

            # Claude bóc nội dung sạch và nhận diện ảnh ngoài; nếu không dùng được
            # thì rơi về bản đã lọc cứng ở trên.
            external_links = []
            ai = ai_extract(decode_mime(msg.get("Subject")), html)
            if ai:
                if ai["title"]:
                    title = clean_title(ai["title"])
                html = ai["content_html"]
                external_links = ai["external_image_links"]

            # Quyết định trạng thái
            status = POST_STATUS
            reason = ""
            if not title or len(strip_tags(html)) < 20:
                status, reason = "draft", "thiếu tiêu đề hoặc nội dung quá ngắn"
            elif external_links and featured is None:
                # ảnh chỉ nằm ngoài (Drive...) và chưa nhúng được ảnh nào
                if DRAFT_WHEN_IMAGES_EXTERNAL:
                    status, reason = "draft", "ảnh nằm trong Drive, cần bỏ ảnh thủ công"
                log(f"   . ảnh ngoài cần xử lý tay: {', '.join(external_links)}")

            if reason:
                log(f" - '{title or '(không tiêu đề)'}' -> draft: {reason}")

            if DRY_RUN:
                log(f" - [DRY] sẽ đăng ({status}): {title[:70]}  | nhúng được ảnh: {'có' if featured else 'không'}, ảnh ngoài: {len(external_links)}")
            else:
                link = wp_create_post(title, html, status, featured, WP_CATEGORY_ID)
                log(f" - đã đăng ({status}): {title[:70]}  -> {link}")

            mark_processed(imap, uid)
            if status == "publish":
                published += 1
            else:
                drafted += 1

        except Exception as e:
            failed += 1
            log(f" ! Mail uid {uid} lỗi, bỏ qua, KHÔNG đánh dấu đã đọc: {e}")
            # cố tình không mark_processed để mai chạy lại

    imap.close()
    imap.logout()
    log(f"Xong. Đăng: {published}, draft: {drafted}, lỗi: {failed}.")
    # nếu có lỗi thì để job đỏ cho dễ thấy, nhưng không chặn các mail đã xong
    if failed and not published and not drafted:
        sys.exit(1)


if __name__ == "__main__":
    main()
