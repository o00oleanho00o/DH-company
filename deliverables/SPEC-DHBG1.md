# Đặc tả yêu cầu DHBG1

## Yêu cầu chức năng

### REQ-DHBG1-001
Hệ thống phải inspect và nhập được workbook `.xls` và `.xlsx`, phát hiện sheet,
header, loại tài liệu và mapping cột mà không phụ thuộc vị trí cố định.

### REQ-DHBG1-002
Mỗi dòng đã đọc phải giữ raw value/formula cùng provenance workbook, sheet và số dòng nguồn.

### REQ-DHBG1-003
Hệ thống phải chuẩn hóa text, dấu, mã, đơn vị, số lượng và thuộc tính kỹ thuật nhưng vẫn giữ mô tả gốc để hiển thị/export.

### REQ-DHBG1-004
Hệ thống phải quản lý catalog vật tư/nhân công, giá phiên bản hóa và lifecycle `ACTIVE`, `ARCHIVED`, `SUPERSEDED`.

### REQ-DHBG1-005
Hệ thống phải matching vật tư và nhân công độc lập theo code/name/token/thuộc tính kỹ thuật/đơn vị.

### REQ-DHBG1-006
Với mỗi loại giá, hệ thống phải xét candidate có điểm cao nhất; chỉ tự áp dụng khi điểm đạt ngưỡng cấu hình, mặc định 90%.

### REQ-DHBG1-007
Một dòng BOQ phải được phép kết hợp material candidate và labor candidate khác nhau; thiếu một phía không được làm mất phía còn lại.

### REQ-DHBG1-008
Người dùng phải xem được candidate, điểm, lý do, provenance và duyệt/chọn candidate khác/nhập giá thủ công trong Bàn rà soát.

### REQ-DHBG1-009
Quotation phải hỗ trợ ba policy: `latest_supplier_net`, `approved_internal`, `historical_median` và lưu policy vào run metadata.

### REQ-DHBG1-010
AI provider chỉ được rerank danh sách candidate đã truy xuất, có timeout/retry/budget, không được tạo giá, công thức hay provenance.

### REQ-DHBG1-011
Export phải chỉ ghi unit price material/labor được áp dụng; giữ nguyên quantity, amount, subtotal, total, style và formula gốc.

### REQ-DHBG1-012
Nguồn đã được tham chiếu phải archive hoặc reprocess thành version mới; xóa cứng phải bị từ chối nếu phá provenance.

### REQ-DHBG1-013
REST API phải cung cấp health, import preview/import, sources, catalog, quotations, review và export với lỗi có cấu trúc.

### REQ-DHBG1-014
UI phải có điều hướng hoạt động giữa Dashboard, Kho dữ liệu, Danh mục & Giá, Báo giá và Bàn rà soát; thao tác chọn item không làm mất context danh sách.

### REQ-DHBG1-015
Server phải mặc định bind `0.0.0.0:3000`, hỗ trợ override bằng biến môi trường và phục vụ tài nguyên bằng URL tương đối.

### REQ-DHBG1-016
Mọi run/review/export phải có audit event đủ để truy ngược input, policy, candidate, source và kết quả.

### REQ-DHBG1-017
UI phải cung cấp chatbot ở hai chế độ: panel nổi dùng trên mọi màn hình và
workspace toàn bộ vùng nội dung khi chọn tab `Chatbot` ở điều hướng trái. Việc
chuyển chế độ phải giữ nguyên lịch sử hội thoại, trạng thái gửi và file đính kèm;
giao diện phải dùng được trên desktop và mobile.

### REQ-DHBG1-018
Chatbot phải trả danh sách artifact tải xuống có `filename` và URL cùng origin;
UI phải dựng liên kết tải trực tiếp sau khi tool export thành công. Tool trạng
thái phải trả explicit counts cho `auto_approved`, `ignored`, `review_required`,
`no_match`, `no_price_found` và không coi dòng `IGNORED` là lỗi.

### REQ-DHBG1-019
Chatbot phải nhận diện mệnh lệnh trực tiếp theo từng write tool và thực thi
trong cùng lượt; câu hỏi hoặc đề xuất không rõ ý phải trả preview và yêu cầu
xác nhận. Không được hỏi lại xác nhận sau một mệnh lệnh rõ ràng.

### REQ-DHBG1-020
BOQ upload tại API tạo báo giá phải được đánh dấu `QUOTATION_INPUT`, bị loại
khỏi danh sách/thống kê/API quản trị Kho dữ liệu và không tạo catalog price/rate.
Workbook do hệ thống xuất, nhận diện bằng sheet `AI Audit`, phải bị từ chối khi
import làm nguồn dữ liệu.

## Yêu cầu phi chức năng

| ID | Yêu cầu | Kiểm chứng |
|---|---|---|
| NFR-DHBG1-001 | Kết quả deterministic khi AI tắt | Test lặp cùng fixture |
| NFR-DHBG1-002 | Không log hoặc trả API key | Security tests + review log |
| NFR-DHBG1-003 | Import/export không làm hỏng ZIP/XML workbook | Regression test mở workbook |
| NFR-DHBG1-004 | Parser và pricing có thể thay provider/storage mà không đổi API | Module contract tests |
| NFR-DHBG1-005 | Upload giới hạn kích thước và dọn file tạm khi lỗi | Security test |
| NFR-DHBG1-006 | LAN/Cloudflare request tới port 3000 hoạt động khi firewall cho phép | Smoke test triển khai |
