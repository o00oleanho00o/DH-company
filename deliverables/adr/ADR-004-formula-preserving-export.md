# ADR-004: Giữ công thức workbook khi export

## Status
Accepted

## Context
Các mẫu BOQ có cột thành tiền/tổng dùng công thức khác nhau, bao gồm công thức cache và nhiều sheet.

## Decision
Sao chép workbook nguồn, chỉ ghi unit price material/labor; giữ nguyên các ô khác và bật native recalculation khi cần.

## Trade-offs
Giá trị cached có thể chưa cập nhật cho tới khi Excel mở file, nhưng cấu trúc và công thức không bị phá.

## Revisit trigger
Nếu cần preview data-only tức thời, bổ sung recalculation service riêng, không sửa logic export hiện tại.
