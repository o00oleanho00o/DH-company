# DH M&E Pricing Hub

Nền tảng web nhập workbook Excel M&E, chuẩn hóa catalog vật tư/nhân công,
matching BOQ, rà soát candidate và xuất báo giá có provenance. Core pipeline
dùng luật deterministic; AI là lớp semantic tùy chọn và không được tạo giá.

## Chạy nhanh

```powershell
python -m pip install -r requirements.txt
python -m app.cli init-db
.\run.ps1
```

Mở trên máy chạy server: `http://127.0.0.1:3000`.

Mở từ máy khác cùng LAN: `http://<IP-của-máy-server>:3000`. Server mặc định
bind `0.0.0.0`; nếu Windows Firewall chặn, cho phép inbound TCP port `3000`.

Public tạm qua Cloudflare Quick Tunnel:

```powershell
cloudflared tunnel --url http://127.0.0.1:3000
```

Production nên dùng named tunnel + Cloudflare Access. Ứng dụng hiện chưa có
RBAC, vì vậy không public trực tiếp cho internet mà không có lớp bảo vệ.

## Cấu hình

Sao chép `.env.example` thành `.env` và giữ file này ngoài Git:

```text
APP_HOST=0.0.0.0
APP_PORT=3000
CLAUDE_BASE_URL=https://api.vilao.ai/v1
CLAUDE_API_KEY=...
CLAUDE_MODEL=occ/claude-opus-4-8
ENABLE_LLM=false
```

`ENABLE_LLM=true` bật reranking semantic cho các candidate mơ hồ. Provider có
timeout, retry, ngân sách call/top-N và chỉ được trả lại thứ tự candidate ID;
không được tính tiền, tạo giá, công thức hoặc nguồn tham chiếu. API key chỉ đọc
server-side, không ghi log và không trả xuống trình duyệt.

## Luồng nghiệp vụ

1. **Kho dữ liệu:** preview/import bảng giá, nhân công và báo giá lịch sử.
2. **Danh mục & Giá:** xem record, nguồn, lịch sử giá và lifecycle.
3. **Tạo báo giá:** upload BOQ, chọn chính sách và chạy pricing.
4. **Bàn rà soát:** kiểm tra candidate, confidence, provenance và correction.
5. **Xuất Excel:** chỉ điền unit price; giữ nguyên quantity, amount và formula.

Ba chính sách giá:

| Policy | Ý nghĩa |
|---|---|
| `latest_supplier_net` | Giá NCC mới nhất sau discount đã cấu hình |
| `approved_internal` | Giá nội bộ đã duyệt gần nhất |
| `historical_median` | Trung vị ba dự án lịch sử hợp lệ gần nhất |

Material và labor được matching/chọn giá độc lập rồi kết hợp trên cùng dòng.
Candidate cao nhất đạt ngưỡng mặc định 90% mới được tự áp dụng; trường hợp mơ
hồ hoặc thiếu giá chuyển sang review.

## API chính

- `GET /api/health`
- `POST /api/import/preview`, `POST /api/import`
- `GET /api/sources`, `GET /api/sources/{id}`
- `GET /api/catalog/items`, `GET /api/catalog/{products|labor}/{id}`
- `POST /api/quotations`, `POST /api/quotations/{id}/run`
- `GET /api/quotations/{id}/review`, `POST /api/boq-items/{id}/review`
- `GET /api/quotations/{id}/export`

## Kiểm thử

```powershell
python -m pytest -q
python -m app.cli benchmark
```

Test bao phủ parser `.xls/.xlsx`, lifecycle/provenance, matching và policy,
AI boundary, security, holdout benchmark và formula-preserving export.

## Kiến trúc và tài liệu

| File | Nội dung |
|---|---|
| `AGENTS.md` | Context bắt buộc cho agent/task sau |
| `docs/DECISIONS.md` | Quyết định đang có hiệu lực |
| `deliverables/SCOPE-DHBG1.md` | Phạm vi và tiêu chí thành công |
| `deliverables/SPEC-DHBG1.md` | REQ/NFR có mã truy vết |
| `deliverables/MODULEMAP-DHBG1.md` | Module sở hữu từng REQ |
| `deliverables/ARCH-DHBG1.md` | Topology, module và luồng dữ liệu |
| `deliverables/adr/` | Quyết định kỹ thuật và trade-off |
| `deliverables/WBS-DHBG1.md` | Công việc dẫn xuất và tiến độ |

### Thứ tự sửa tài liệu

```text
DECISIONS → SCOPE → SPEC → MODULEMAP → ARCH → ADR → WBS
```

WBS luôn sửa cuối cùng. Sau khi sửa, kiểm tra mọi REQ trong SPEC có đúng một
module sở hữu, module xuất hiện trong ARCH và các task WBS có REQ/module rõ ràng.

## Cấu trúc code

```text
app/
  main.py          FastAPI + REST + static UI
  excel.py         workbook inspection/mapping
  ingest.py        canonical ingestion + provenance
  normalize.py     text/unit/technical attributes
  pricing.py       retrieval, scoring, run và review
  price_policy.py  material pricing policy
  labor_policy.py  labor pricing policy
  ai.py            optional bounded reranker
  export.py        formula-preserving XLSX export
  db.py            SQLite schema/session
  static/          Vietnamese web UI
tests/             unit/integration/regression tests
benchmarks/        leakage-safe holdout reports
```

## Giới hạn production

Trước khi dùng internet-facing lâu dài cần authentication/RBAC, HTTPS/Access
policy, PostgreSQL, worker queue, object storage, concurrency control và
observability. Xem `KNOWN_LIMITATIONS.md` và kiến trúc chi tiết trong
`deliverables/ARCH-DHBG1.md`.
