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
- `app/export.py`: chỉ ghi ô unit price; native Excel được yêu cầu recalculation cho workbook nhiều sheet.
- `app/main.py`: API, upload lifecycle và static UI.

## Luồng request

1. Upload được lưu dưới thư mục tạm và kiểm tra kích thước.
2. Parser tạo snapshot; ingest ghi raw/provenance và canonical records.
3. Pricing truy xuất candidate theo từng loại giá, có thể gọi AI rerank nếu được bật.
4. Run ghi metrics, candidate evidence và audit; dòng rủi ro đi vào review.
5. Export sao chép workbook nguồn rồi chỉ cập nhật material/labor unit-price cells.

## Triển khai mạng

- Dev/local: `python -m uvicorn app.main:app --host 0.0.0.0 --port 3000 --reload`.
- LAN: truy cập `http://<IP-máy-chạy>:3000`; mở inbound TCP 3000 trên Windows Firewall nếu cần.
- Cloudflare: chạy `cloudflared tunnel --url http://127.0.0.1:3000` trên cùng máy; không expose SQLite/raw storage trực tiếp.

## An toàn và độ tin cậy

- `.env` chỉ được đọc server-side và không trả key.
- Giá không có provenance không được áp dụng.
- Nguồn đã tham chiếu không xóa cứng.
- Workbook export được kiểm tra ZIP/XML và regression bằng openpyxl.
