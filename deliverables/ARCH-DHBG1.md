# Kiến trúc DHBG1

## Tổng quan

```text
Browser/LAN/Cloudflare
          │ HTTP :3000
          ▼
FastAPI modular monolith (app/main.py)
  ├─ intake + excel
  ├─ normalize + ingest
  ├─ catalog/provenance (db)
  ├─ pricing + price policy + labor policy
  ├─ optional AI reranker
  ├─ review API
  └─ formula-preserving export
          │
          ├─ SQLite storage/pricing.db
          ├─ storage/raw (immutable snapshots)
          └─ storage/exports
```

## Thiết kế module

- `app/excel.py`: đọc snapshot cached/formula, phát hiện loại sheet và mapping cột.
- `app/normalize.py`: canonical text/unit/number và thuộc tính kỹ thuật.
- `app/ingest.py`: idempotent import, catalog links, observations và provenance.
- `app/pricing.py`: candidate scoring, threshold 90%, exact historical reference, policy và review state.
- `app/price_policy.py`/`app/labor_policy.py`: chọn giá theo policy, discount/tax và thống kê.
- `app/ai.py`: OpenAI-compatible provider opt-in; bounded rerank, không tính toán.
- `app/chatbot.py`: assistant provider và tool boundary riêng cho hội thoại hỗ trợ vận hành.
- `app/export.py`: chỉ ghi ô unit price; native Excel được yêu cầu recalculation cho workbook nhiều sheet.
- `app/main.py`: API, upload lifecycle và static UI.
- `app/static/chatbot.js`: sở hữu một conversation state và một chat surface. Surface được chuyển giữa widget nổi và `#chatbot-page-host` theo route `#chatbot`, không clone DOM và không reset hội thoại.
- `app/chatbot.py` + `/api/chatbot`: tool loop trả `downloads` metadata cho artifact export và trạng thái quotation theo nhóm explicit; `app/static/chatbot.js` dựng nút tải cùng origin.
- Direct-intent detector trong `app/chatbot.py` chỉ nâng `confirm=true` cho cụm từ hành động rõ ràng và đúng tool; mặc định vẫn là preview.
- `source_files` giữ snapshot `QUOTATION_INPUT` cho provenance/export, nhưng source APIs chỉ phục vụ `REFERENCE`; parser chặn workbook export có sheet `AI Audit` khỏi luồng import.

## Chatbot hai chế độ

- `quick`: panel cố định góc phải, phù hợp câu hỏi ngắn trên các workflow khác.
- `workspace`: tab điều hướng `Chatbot` đưa cùng panel vào vùng nội dung, mở rộng chiều ngang/cao và ẩn nút nổi.
- Route change phát custom event để `chatbot.js` chuyển surface; mở trực tiếp `#chatbot` vẫn được xử lý khi khởi tạo.
- Đóng workspace điều hướng về màn trước hoặc Tổng quan; lịch sử chỉ xóa khi người dùng bấm làm mới hội thoại.

## Luồng request

1. Upload được lưu dưới thư mục tạm và kiểm tra kích thước.
2. Parser tạo snapshot; ingest ghi raw/provenance và canonical records.
3. Pricing truy xuất candidate theo từng loại giá, có thể gọi AI rerank nếu được bật.
4. Run ghi metrics, candidate evidence và audit; dòng rủi ro đi vào review.
5. Export sao chép workbook nguồn rồi chỉ cập nhật material/labor unit-price cells.
6. Chatbot response mang artifact metadata; trình duyệt tải qua `FileResponse` của API export, không dùng URL do model tự viết.
7. Tạo báo giá lưu snapshot BOQ nội bộ với role `QUOTATION_INPUT`; Kho dữ liệu chỉ liệt kê nguồn `REFERENCE`.

## Triển khai mạng

- Dev/local: `python -m uvicorn app.main:app --host 0.0.0.0 --port 3000 --reload`.
- LAN: truy cập `http://<IP-máy-chạy>:3000`; mở inbound TCP 3000 trên Windows Firewall nếu cần.
- Cloudflare: chạy `cloudflared tunnel --url http://127.0.0.1:3000` trên cùng máy; không expose SQLite/raw storage trực tiếp.

## An toàn và độ tin cậy

- `.env` chỉ được đọc server-side và không trả key.
- Giá không có provenance không được áp dụng.
- Nguồn đã tham chiếu không xóa cứng.
- Workbook export được kiểm tra ZIP/XML và regression bằng openpyxl.
