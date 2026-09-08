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

### Trợ lý AI (chatbot bubble)

Widget floating góc dưới bên phải giao diện — hoạt động như một "mini app"
trong khung chat: vừa trả lời Q&A dựa trên `prompt_system.txt`, vừa có thể
gọi tool để đọc/ghi dữ liệu thật (xem nguồn, tra catalog, tạo báo giá, chạy
pricing, duyệt rà soát, xuất Excel...). Lệnh trực tiếp như "Xuất Excel" hoặc
"Áp giá đi" là xác nhận thực thi khi đã rõ báo giá và thông số; trợ lý chỉ hỏi
thêm khi thiếu thông tin hoặc người dùng mới yêu cầu xem trước. Xem giới hạn ở đầu
`app/chatbot.py`. Hoàn toàn tách biệt khỏi core pricing engine (deterministic).

```text
CHATBOT_BASE_URL=https://api.deepseek.com
CHATBOT_API_KEY=
CHATBOT_MODEL=deepseek-v4-pro
ENABLE_CHATBOT=true
```

- Provider OpenAI-compatible (DeepSeek), gọi qua SDK `openai` chính thức
  (`app/chatbot.py`), có bật chế độ reasoning (`reasoning_effort="high"`,
  `extra_body={"thinking": {"type": "enabled"}}`).
- `CHATBOT_API_KEY` bắt buộc để trả lời thật; thiếu key → widget vẫn hiện
  nhưng trả lỗi thân thiện, không lộ chi tiết provider. Key chỉ đọc
  server-side, không log, không trả xuống trình duyệt.
- Sửa nội dung chatbot biết bằng cách chỉnh `prompt_system.txt` rồi khởi động
  lại server (file được nạp một lần lúc start). System prompt viết bằng
  tiếng Anh (tiết kiệm token) nhưng luôn ép trả lời cuối cùng bằng tiếng
  Việt, và chỉ xử lý yêu cầu liên quan đến app này — câu hỏi ngoài phạm vi
  (giá vàng, chứng khoán,...) sẽ bị từ chối ngắn gọn thay vì được trả lời.
- Hội thoại được lưu trong `localStorage` của trình duyệt (chỉ máy người
  dùng, không lên server) nên tải lại trang là thấy lại ngay; nút "Làm mới
  hội thoại" xoá cả bộ lưu này.
- Không có session phía server: mỗi request gửi lại toàn bộ lịch sử. 24 lượt
  gần nhất được gửi nguyên văn, các lượt cũ hơn được **gấp thành một đoạn tóm
  tắt ngắn** (thay vì bị xoá như trước) nên chat dài không mất ngữ cảnh. Tóm
  tắt được cache trong tiến trình theo block 8 tin, nên thường không phát
  sinh lệnh gọi AI phụ; nếu tóm tắt lỗi thì tự rơi về hành vi cũ (bỏ lượt cũ)
  chứ không làm hỏng câu trả lời.
- `ENABLE_CHATBOT=false` ẩn hẳn widget.

## Luồng nghiệp vụ

1. **Kho dữ liệu:** preview/import bảng giá, nhân công và báo giá lịch sử.
2. **Danh mục & Giá:** xem record, nguồn, lịch sử giá và lifecycle.
3. **Tạo báo giá:** upload BOQ, chọn chính sách và chạy pricing.
4. **Bàn rà soát:** kiểm tra candidate, confidence, provenance và correction.
5. **Xuất Excel:** chỉ điền unit price; giữ nguyên quantity, amount và formula.

Ba chính sách giá:

| Policy                  | Ý nghĩa                                          |
| ----------------------- | -------------------------------------------------- |
| `latest_supplier_net` | Giá NCC mới nhất sau discount đã cấu hình   |
| `approved_internal`   | Giá nội bộ đã duyệt gần nhất               |
| `historical_median`   | Trung vị ba dự án lịch sử hợp lệ gần nhất |

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

## Khám phá code với Understand-Anything

Understand-Anything (bên thứ ba, MIT) dựng knowledge graph tương tác cho codebase —
sơ đồ kiến trúc, guided tour, semantic search và phân tích tác động thay đổi. Không
bắt buộc để chạy hoặc dev app; chỉ hỗ trợ người mới/agent hiểu nhanh cấu trúc code.

Repo: https://github.com/Egonex-AI/Understand-Anything

Yêu cầu Node.js ≥ 18 và [pnpm](https://pnpm.io/) (dùng để build gói `core`/dashboard
của plugin ở lần chạy đầu).

### Cài đặt — dùng Claude Code

```
/plugin marketplace add Egonex-AI/Understand-Anything
/plugin install understand-anything
```

Khởi động lại Claude Code để các skill mới nạp.

### Cài đặt — không dùng Claude Code

Plugin cũng hỗ trợ Codex, OpenCode, Cursor, VS Code Copilot, Gemini CLI và nhiều
agent CLI khác qua script cài đặt riêng (clone repo về `~/.understand-anything/repo`
và tạo symlink/junction skill vào thư mục agent tương ứng):

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/Egonex-AI/Understand-Anything/main/install.sh | bash
```

Windows (PowerShell):

```powershell
iwr -useb https://raw.githubusercontent.com/Egonex-AI/Understand-Anything/main/install.ps1 | iex
```

Script sẽ hỏi chọn platform (hoặc truyền sẵn, ví dụ `install.ps1 codex`); chạy lại
với `-Update`/`--update` để pull bản mới, `-Uninstall <platform>`/`--uninstall <platform>` để gỡ. Sau khi cài, agent tương ứng sẽ nhận diện các lệnh `/understand*`
như một skill thông thường — không cần khởi động lại toàn bộ máy, chỉ cần agent
nạp lại danh sách skill (thường là mở phiên làm việc mới).

### Sử dụng

```
/understand                    # phân tích code, sinh .ua/knowledge-graph.json
/understand-dashboard          # mở dashboard tương tác (Vite dev server local)
/understand-chat <câu hỏi>     # hỏi đáp về codebase dựa trên knowledge graph
/understand-diff                # phân tích tác động của một thay đổi/diff
/understand-domain              # trích xuất business logic thành domain graph
```

`/understand` hỏi ngôn ngữ output (chọn `vi` cho dự án này) và sinh file
`.understandignore` để review trước khi phân tích toàn bộ — nên loại `input/`
(workbook Excel thật) khỏi phạm vi quét. Toàn bộ artifact nằm trong `.ua/`
(đã có trong `.gitignore`, không commit).

## Kiến trúc và tài liệu

| File                                | Nội dung                              |
| ----------------------------------- | -------------------------------------- |
| `AGENTS.md`                       | Context bắt buộc cho agent/task sau  |
| `docs/DECISIONS.md`               | Quyết định đang có hiệu lực     |
| `deliverables/SCOPE-DHBG1.md`     | Phạm vi và tiêu chí thành công   |
| `deliverables/SPEC-DHBG1.md`      | REQ/NFR có mã truy vết              |
| `deliverables/MODULEMAP-DHBG1.md` | Module sở hữu từng REQ              |
| `deliverables/ARCH-DHBG1.md`      | Topology, module và luồng dữ liệu  |
| `deliverables/adr/`               | Quyết định kỹ thuật và trade-off |
| `deliverables/WBS-DHBG1.md`       | Công việc dẫn xuất và tiến độ  |

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
  chatbot.py       assistant riêng cho hỗ trợ vận hành
  export.py        formula-preserving XLSX export
  db.py            SQLite schema/session
  static/          Vietnamese web UI + chatbot panel nổi/workspace toàn màn hình
tests/             unit/integration/regression tests
benchmarks/        leakage-safe holdout reports
prompt_system.txt  system prompt + knowledge base cho chatbot widget
```

## Giới hạn production

Trước khi dùng internet-facing lâu dài cần authentication/RBAC, HTTPS/Access
policy, PostgreSQL, worker queue, object storage, concurrency control và
observability. Xem `KNOWN_LIMITATIONS.md` và kiến trúc chi tiết trong
`deliverables/ARCH-DHBG1.md`.
