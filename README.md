# TCBC tự động lên WordPress (chạy 1 lần/ngày bằng GitHub Actions)

Bộ này gồm ba file:
- `press_to_wp.py`: script làm toàn bộ việc.
- `daily.yml`: lịch chạy của GitHub. Đặt vào đúng đường dẫn `.github/workflows/daily.yml`.
- `requirements.txt`: thư viện cần cài.

## Lắp ráp trong 5 bước

1. Tạo một repo riêng (để private). Bỏ ba file vào, nhớ `daily.yml` phải nằm trong thư mục `.github/workflows/`.

2. Lấy mật khẩu cho hộp mail. Với Gmail phải bật 2FA rồi tạo một App Password 16 ký tự (không dùng được mật khẩu thường). Host IMAP của Gmail là `imap.gmail.com`.

3. Lấy mật khẩu WordPress. Vào WP Admin > Users > Profile > Application Passwords, tạo một cái mới, copy chuỗi nó hiện ra (chỉ hiện một lần). Đây không phải mật khẩu đăng nhập thường ngày. Site phải chạy HTTPS.

4. Vào repo > Settings > Secrets and variables > Actions > New repository secret, thêm lần lượt:

   | Tên | Bắt buộc | Ghi chú |
   |---|---|---|
   | `IMAP_HOST` | có | vd `imap.gmail.com` |
   | `IMAP_USER` | có | địa chỉ hộp mail riêng |
   | `IMAP_PASS` | có | App Password của mail |
   | `IMAP_FOLDER` | không | mặc định `INBOX` |
   | `PROCESSED_FOLDER` | không | vd `Done`, để trống thì chỉ đánh dấu đã đọc |
   | `WP_URL` | có | vd `https://anbeauty.vn` |
   | `WP_USER` | có | username WordPress |
   | `WP_APP_PASSWORD` | có | Application Password của WP |
   | `WP_CATEGORY_ID` | không | id chuyên mục muốn gán |
   | `POST_STATUS` | không | `publish` (mặc định) hoặc `draft` |
   | `ANTHROPIC_API_KEY` | không | bật bước dọn rác mail forward |
   | `ANTHROPIC_MODEL` | không | mặc định `claude-haiku-4-5-20251001` |
   | `AI_CLEANUP` | không | `true`/`false`, mặc định `true` nếu có API key |

5. Test trước khi thả thật. Vào tab Actions > chọn workflow > Run workflow, điền `dry_run` = `true`. Nó sẽ in ra sẽ-đăng-gì mà không đụng vào WordPress hay hộp mail. Xem log thấy ổn thì chạy lại với `dry_run` = `false`.

## Vài điều đã cài sẵn

- Trạng thái nằm trong hộp mail: mail xử lý xong bị đánh dấu đã đọc (và dời sang Done nếu khai báo), nên không bao giờ đăng trùng.
- Van an toàn: mail nào parse ra mà thiếu tiêu đề hoặc thân rỗng thì tự hạ về draft thay vì đăng.
- Mail nào lỗi giữa chừng thì bỏ qua và KHÔNG đánh dấu đã đọc, để hôm sau chạy lại, không kéo sập cả mẻ.
- Bước dọn bằng Claude có chốt chặn: nếu model lỡ làm rỗng nội dung hoặc đánh rơi ảnh thì script tự quay về dùng bản HTML gốc.

## Hai lưu ý về GitHub Actions

- Lịch cron không chính xác tới từng phút, có lúc trễ vài chục phút. Blog không gấp nên không sao.
- GitHub tự tạm dừng workflow hẹn giờ nếu repo nằm im quá 60 ngày. Thỉnh thoảng commit một cái cho nó tỉnh.
