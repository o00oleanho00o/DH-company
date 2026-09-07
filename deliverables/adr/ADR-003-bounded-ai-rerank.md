# ADR-003: AI chỉ rerank candidate có giới hạn

## Status
Accepted

## Context
Semantic similarity giúp xử lý mô tả Excel không đồng nhất nhưng model không được quyết định giá thương mại.

## Decision
Deterministic engine truy xuất candidate; provider tùy chọn chỉ trả thứ tự ID trong top-N, có timeout/retry/budget và audit.

## Trade-offs
Giảm rủi ro hallucination nhưng không xử lý mọi dòng mơ hồ tự động.

## Revisit trigger
Chỉ mở rộng autonomy khi có benchmark, approval và audit chứng minh an toàn.
