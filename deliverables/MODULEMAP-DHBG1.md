# Module map DHBG1

## Bảng module

| Module | Trách nhiệm | REQ sở hữu |
|---|---|---|
| `runtime-config` | env, host/port, storage path, feature flags | REQ-DHBG1-015 |
| `intake` | inspect, classify, upload, raw snapshot | REQ-DHBG1-001 |
| `normalization` | text/unit/number/technical attributes, row kind | REQ-DHBG1-003 |
| `provenance-store` | SQLite schema, source lifecycle, audit links | REQ-DHBG1-002, REQ-DHBG1-004, REQ-DHBG1-012, REQ-DHBG1-016 |
| `pricing-engine` | retrieval, scoring, policy, independent material/labor combination | REQ-DHBG1-005, REQ-DHBG1-006, REQ-DHBG1-007, REQ-DHBG1-009 |
| `ai-provider` | bounded OpenAI-compatible reranking | REQ-DHBG1-010 |
| `review-workspace` | candidate review, correction and status | REQ-DHBG1-008 |
| `quotation-api` | REST orchestration and error contract | REQ-DHBG1-013 |
| `excel-export` | price-cell writes, formula/cache preservation, AI Audit | REQ-DHBG1-011 |
| `web-ui` | Vietnamese SPA navigation and workflow screens | REQ-DHBG1-014 |

## Luồng chính

`web-ui → quotation-api → intake → normalization → provenance-store → pricing-engine → ai-provider (optional) → review-workspace → excel-export`

## Ranh giới dữ liệu

- `intake` không quyết định giá.
- `pricing-engine` không ghi raw workbook.
- `ai-provider` không được truy cập database trực tiếp và không nhận secret.
- `excel-export` đọc snapshot nguồn và kết quả run, không tự tính lại nghiệp vụ.
