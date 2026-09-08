# WBS DHBG1

WBS được dẫn xuất từ `docs/DECISIONS.md`, `SCOPE-DHBG1.md`, `SPEC-DHBG1.md`,
`MODULEMAP-DHBG1.md`, `ARCH-DHBG1.md` và các ADR. Mỗi task phải có test hoặc
bằng chứng kiểm chứng trước khi đánh dấu hoàn thành.

## Tiến độ

| Trạng thái | Số việc |
|---|---:|
| ☑ Hoàn thành trong repo | 10 |
| ◐ Đang cần người dùng xác nhận | 3 |
| ☐ Chưa làm | 8 |
| **Tổng** | **21** |

## Nền tảng và dữ liệu

| ID | Việc | Module | REQ | Trạng thái |
|---|---|---|---|---|
| 1.1 | Chuẩn hóa config/env và port 3000 | `runtime-config` | 015 | ☑ |
| 1.2 | Schema SQLite, audit và provenance | `provenance-store` | 002,004,012,016 | ☑ |
| 1.3 | Inspect/import `.xls`/`.xlsx` | `intake` | 001 | ☑ |
| 1.4 | Normalize và classify rows | `normalization` | 003 | ☑ |
| 1.5 | Catalog lifecycle/archive/reprocess | `provenance-store` | 004,012 | ☑ |

## Pricing và AI

| ID | Việc | Module | REQ | Trạng thái |
|---|---|---|---|---|
| 2.1 | Matching material/labor độc lập | `pricing-engine` | 005,007 | ☑ |
| 2.2 | Threshold 90% và chọn candidate cao nhất | `pricing-engine` | 006 | ☑ |
| 2.3 | Ba pricing policy và discount/tax | `pricing-engine` | 009 | ◐ |
| 2.4 | Bounded AI rerank và usage audit | `ai-provider` | 010 | ☑ |
| 2.5 | Benchmark leakage-safe và regression | `pricing-engine` | 006,009,010 | ☑ |

## Review, API và export

| ID | Việc | Module | REQ | Trạng thái |
|---|---|---|---|---|
| 3.1 | Review candidate/correction/provenance | `review-workspace` | 008,016 | ☑ |
| 3.2 | REST API health/import/catalog/quotation | `quotation-api` | 013 | ☑ |
| 3.3 | UI điều hướng và giữ context list/detail | `web-ui` | 014 | ◐ |
| 3.4 | Export chỉ ghi unit price, giữ formula | `excel-export` | 011 | ☑ |
| 3.5 | Kiểm thử ZIP/XML/openpyxl | `excel-export` | 011, NFR-003 | ☑ |
| 3.6 | Chatbot panel nổi + workspace toàn màn hình dùng chung session | `web-ui` | 017 | ◐ |
| 3.7 | Artifact tải Excel có cấu trúc và status semantics không suy diễn | `chatbot-api`, `web-ui` | 018 | ☑ |

## Vận hành và bàn giao

| ID | Việc | Module | REQ | Trạng thái |
|---|---|---|---|---|
| 4.1 | Smoke test truy cập LAN `:3000` | `runtime-config` | 015, NFR-006 | ☐ |
| 4.2 | Hướng dẫn Cloudflare Tunnel và firewall | `runtime-config` | 015, NFR-006 | ☐ |
| 4.3 | Auth/RBAC trước internet production | `quotation-api` | Scope out | ☐ |
| 4.4 | PostgreSQL/worker/object storage production | `provenance-store` | Scope out | ☐ |
| 4.5 | Chạy full suite và review acceptance | — | NFR-001..006 | ☐ |

## Quy tắc cập nhật

1. Không đổi WBS trước khi tầng quyết định/spec/kiến trúc đã cập nhật.
2. Mỗi task ghi rõ module và REQ liên quan.
3. Task hoàn thành phải có test/bằng chứng; người dùng xác nhận các mục cần review.
