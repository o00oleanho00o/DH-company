# ADR-006: Một chat surface cho hai chế độ hiển thị

## Status
Accepted

## Context
Chatbot cần vừa hiện nhanh trên mọi màn hình, vừa có workspace rộng để đọc và thao tác lâu. Hai instance riêng sẽ làm phân kỳ lịch sử, request state và file đính kèm.

## Decision
Dùng một DOM chat panel và một conversation state. Khi route là `#chatbot`, panel được chuyển vào vùng nội dung; khi rời route, panel quay về widget nổi.

## Trade-offs
Giữ state tự nhiên và không duplicate event handler, nhưng route renderer và chatbot module phải giao tiếp qua một custom event ổn định.

## Revisit trigger
Nếu hội thoại được persist server-side hoặc hỗ trợ nhiều conversation đồng thời, thay state in-memory bằng conversation store có ID.
