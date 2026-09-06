# DH M&E Pricing Hub

Nền tảng web local để nhập các workbook Excel doanh nghiệp, chuẩn hóa dữ liệu
vật tư/nhân công, matching BOQ và xuất báo giá có provenance. Đây là MVP
chạy thật, không phải chatbot: core pipeline dùng parser, database và luật
deterministic; AI semantic là provider tùy chọn.

## Chạy nhanh (Windows)

```powershell
# Dùng Python đã cài dependency
python -m pip install -r requirements.txt

# Khởi tạo database
python -m app.cli init-db

# (Tuỳ chọn) nhập 5 nguồn training trong input; giữ BOQ Trại Lợn ngoài operational DB
python -m app.cli seed

# Chạy web
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Mở `http://127.0.0.1:8000`. Script tương đương là `.\run.ps1`.

Runtime cần `xlrd==2.0.1` để đọc `.xls` BIFF; `openpyxl` đọc `.xlsx`.
Không cần PostgreSQL cho local MVP. Schema canonical nằm trong
`app/db.py`, còn hướng migration production nằm ở `migrations/001_initial.sql`.

## Cấu hình API key

Ứng dụng đọc các biến dotenv-style đầu tiên trong `.env` hiện có:

```text
CLAUDE_BASE_URL=https://api.vilao.ai/v1
CLAUDE_API_KEY=...
CLAUDE_MODEL=occ/claude-opus-4-8
ENABLE_LLM=false
```

`ENABLE_LLM=false` giữ pipeline deterministic và không phát sinh gọi API. Đặt
`ENABLE_LLM=true` khi muốn bật reranking semantic; key không được ghi log hoặc
gửi xuống trình duyệt. `.env` đã được thêm vào `.gitignore`. Mặc định LLM chỉ
được gọi khi hai candidate đứng đầu có điểm gần nhau; model chỉ được phép sắp
xếp các ID đã cung cấp, không được tạo giá hay nguồn giá.

Có thể tinh chỉnh lớp semantic bằng các biến sau (không cần đổi code):

```text
LLM_TIMEOUT_SECONDS=30   # timeout mỗi HTTP request
LLM_MAX_CALLS=20         # ngân sách lượt gọi trong một batch/provider
LLM_MAX_CANDIDATES=10    # số candidate tối đa gửi vào prompt
LLM_MAX_RETRIES=1        # chỉ retry lỗi tạm thời (429/5xx/timeout)
LLM_MAX_PROMPT_CHARS=12000
LLM_MAX_TOKENS=800
LLM_RERANK_MARGIN=0.045  # chỉ rerank khi chênh lệch điểm nhỏ hơn ngưỡng
```

Các benchmark leakage-safe nên giữ `ENABLE_LLM=false` hoặc truyền policy
`{"llm_enabled": false}` để số đo deterministic và tái lập. Khi bật semantic
trong môi trường vận hành, hãy theo dõi `GET /api/health` (chỉ báo
`api_key_present`, không trả key) và luôn giữ bước kỹ sư review cho các trường
hợp model yêu cầu xem lại.

## Luồng sử dụng

1. **Kho dữ liệu:** kéo thả bảng giá, nhân công và báo giá lịch sử. Hệ thống
   inspect sheet/header, tự phân loại và giữ raw workbook/raw cells.
2. **Tạo báo giá:** upload BOQ mới, chọn ngày/chính sách, chạy pricing.
3. **Kết quả:** xem coverage, giá vật tư/nhân công, confidence và provenance.
4. **Bàn rà soát:** duyệt candidate, chọn candidate khác, nhập giá thủ công có
   ghi nguồn, yêu cầu báo giá NCC hoặc bỏ qua. Correction được lưu.
5. **Xuất Excel:** `.xlsx` có dữ liệu báo giá và sheet `AI Audit`.

### Kiểm soát vòng đời dữ liệu

Mỗi workbook có trạng thái `ACTIVE`, `ARCHIVED` hoặc `SUPERSEDED`. Khi bảng
giá lỗi thời, hãy archive hoặc nạp lại để tạo phiên bản mới; không xóa cứng
nguồn đã được dùng trong báo giá. Các bảng `price_observations` và
`catalog_source_links` giữ liên kết tới workbook/sheet/dòng để có thể truy
ngược và giải thích giá đã áp dụng. Record vật tư/nhân công trong **Danh mục &
giá** mặc định chỉ hiển thị trạng thái `ACTIVE`; dùng `status=all` để kiểm tra
những record đã archive.

Các API chính:

- `GET /api/health`
- `POST /api/import/preview`
- `POST /api/import`
- `POST /api/seed`
- `GET /api/sources`
- `GET /api/sources/{id}` (chi tiết sheet, mapping, raw rows, catalog, giá, BOQ và provenance)
- `POST /api/sources/{id}/archive`
- `POST /api/sources/{id}/restore`
- `DELETE /api/sources/{id}` (nguồn đã tham chiếu sẽ trả `409`, không phá lịch sử)
- `POST /api/sources/{id}/reprocess` (tạo version mới, giữ version cũ ở `SUPERSEDED`)
- `GET /api/catalog/stats`
- `GET /api/catalog/items?kind=product|labor&category=...&source_id=...&status=...&q=...`
- `GET /api/catalog/products/{id}`
- `GET /api/catalog/labor/{id}`
- `POST/DELETE /api/catalog/items/{kind}/{id}` (archive/restore/xóa an toàn)
- `POST /api/quotations`
- `POST /api/quotations/{id}/run`
- `GET /api/quotations/{id}/review`
- `POST /api/boq-items/{id}/review`
- `GET /api/quotations/{id}/export`
- `GET /api/benchmark`

## Benchmark leakage-safe

```powershell
python -m app.cli benchmark
# hoặc
python -m benchmarks.holdout
```

Benchmark tạo SQLite tạm, ingest các workbook training, rồi ingest riêng
`BOQ-HỆ THỐNG ĐIỆN TRẠI LƠN HẢI HÀ-DH290124.xlsx` với
`exclude_prices=True`, chạy pricing và ghi. Lệnh `seed` mặc định không đưa
workbook holdout vào operational database. Nếu muốn nạp toàn bộ file (và
không giữ holdout), dùng `python -m app.cli seed --no-holdout`; còn luồng demo
nên upload holdout qua màn **Tạo báo giá**.

- `benchmarks/holdout-report.json`
- `benchmarks/holdout-report.md`
- `benchmarks/round2-report.json`
- `benchmarks/round2-report.md`
- `ROUND2-FINAL-REPORT.md`

Giá holdout không được chèn vào `product_prices`/`labor_rates`; report có
leakage guard, parsing metrics, coverage, sai số giá, false-positive ở
confidence cao và provenance completeness. Kết quả baseline phải được đọc
trung thực; target KPI trong prompt không được coi là số đạt được nếu report
chưa chứng minh.

Round 2 còn sinh `data-gap-report.json/.md`, `failure-analysis.md`,
`temporal-modes-report.json/.md` và `row-classification.json/.md`. Các metric
`source_supported` là upper bound theo category/catalog, không phải cam kết
đúng SKU. Benchmark deterministic vẫn là baseline; semantic reranking chỉ
được bật tường minh bằng `--enable-llm` và bị giới hạn ngân sách.

Nếu doanh nghiệp đã xác nhận chính sách thương mại, có thể khai báo discount
theo supplier/category/product family trong `.env` bằng
`MANUAL_PRICING_RULES_JSON`. Khi chưa có xác nhận, hệ thống giữ list ex-VAT và
ghi rõ lý do thay vì tự đoán net price.

## Kiểm thử

```powershell
python -m pytest -q
```

Test bao gồm parser `.xls/.xlsx`, header/column generalization, số/đơn vị,
technical attributes, idempotent ingest, provenance, holdout isolation,
candidate scoring và end-to-end pricing.

## Cấu trúc chính

```text
app/
  main.py       # FastAPI + REST
  excel.py      # workbook adapters/inspection/mapping
  ingest.py     # canonical ingestion + provenance
  pricing.py    # retrieval/matching/policy/review
  export.py     # XLSX result + AI Audit
  ai.py         # optional OpenAI-compatible provider
  db.py         # SQLite schema and sessions
  static/       # Vietnamese enterprise UI
benchmarks/     # leakage-safe holdout runner/report
tests/          # real workbook/unit/integration tests
input/          # six supplied workbooks
```

## Trạng thái production

MVP này đã có end-to-end local workflow. Trước production cần bổ sung
authentication/RBAC, PostgreSQL + pgvector, worker queue, object storage,
concurrency controls, catalog approval governance, recalculation service cho
formula-heavy workbooks và benchmark mở rộng theo từng project.
