# ADR-001: FastAPI modular monolith

## Status
Accepted

## Context
MVP cần đi từ upload đến export trên một máy, team nhỏ và chưa có nhu cầu scale độc lập.

## Decision
Dùng một FastAPI application, chia module theo ranh giới nghiệp vụ và gọi nội bộ qua function contracts.

## Trade-offs
Đơn giản triển khai và debug, nhưng một process failure ảnh hưởng toàn hệ thống.

## Revisit trigger
Chỉ tách service khi có tải độc lập, owner riêng và bằng chứng vận hành cần thiết.
