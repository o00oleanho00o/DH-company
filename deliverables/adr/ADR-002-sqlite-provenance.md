# ADR-002: SQLite và provenance-first storage

## Status
Accepted

## Context
Giá báo cần tái lập và giải thích được, trong khi MVP cần zero-setup.

## Decision
SQLite là operational store; raw workbook giữ riêng; mọi canonical record lưu source file/sheet/row.

## Trade-offs
Giới hạn concurrent write, đổi lại portability và reproducibility cao.

## Revisit trigger
Khi nhiều người dùng ghi đồng thời hoặc cần HA, chuyển schema sang PostgreSQL.
