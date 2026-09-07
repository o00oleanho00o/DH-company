# ADR-005: Bind `0.0.0.0:3000`

## Status
Accepted

## Context
Người dùng cần review từ máy khác và public web qua Cloudflare.

## Decision
Mặc định `APP_HOST=0.0.0.0`, `APP_PORT=3000`; cho phép override bằng environment.

## Trade-offs
Dễ truy cập LAN/tunnel hơn nhưng phải kiểm soát firewall, tunnel và authentication.

## Revisit trigger
Trước production internet-facing phải thêm auth/RBAC, HTTPS policy và managed database.
