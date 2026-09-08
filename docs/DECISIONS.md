# Quyết định dự án DH-Baogia

Tài liệu này là tầng quyết định nghiệp vụ/phạm vi. Mọi thay đổi tiếp theo phải
được phản ánh theo thứ tự: `DECISIONS → SCOPE → SPEC → MODULEMAP → ARCH → ADR → WBS`.

## QĐ-01 — Chọn modular monolith cho MVP

- **Người quyết:** Principal AI Engineer / Tech Lead
- **Ngày:** 2026-09-07
- **Trạng thái:** Accepted
- **Quyết định:** Chạy FastAPI trên một tiến trình, chia ranh giới theo module nghiệp vụ; không tách microservice.
- **Lý do:** Quy mô hiện tại nhỏ, cần triển khai nhanh tại máy nội bộ, dữ liệu và transaction chưa cần scale độc lập.
- **Đánh đổi:** Một tiến trình là điểm lỗi chung; bù lại giảm vận hành và dễ kiểm thử đầu-cuối.

## QĐ-02 — SQLite là kho vận hành mặc định

- **Quyết định:** SQLite là database mặc định cho local/MVP; schema và migration giữ đường nâng cấp PostgreSQL.
- **Lý do:** Không yêu cầu cài dịch vụ ngoài, phù hợp dữ liệu báo giá và kiểm thử tái lập.
- **Đánh đổi:** Khả năng ghi đồng thời và mở rộng thấp hơn PostgreSQL.

## QĐ-03 — Giá deterministic, AI chỉ rerank có giới hạn

- **Quyết định:** Parser, chuẩn hóa, matching, chính sách giá, tính tiền và provenance do code quyết định. AI provider chỉ được sắp xếp lại candidate đã có, theo ngân sách và phải ghi audit.
- **Lý do:** Không cho mô hình tự tạo giá, công thức hoặc nguồn tham chiếu.
- **Đánh đổi:** Một số dòng mơ hồ vẫn cần con người rà soát.

## QĐ-04 — Provenance và vòng đời nguồn là bắt buộc

- **Quyết định:** Mọi giá/BOQ giữ liên kết workbook → sheet → row; nguồn đã tham chiếu không bị xóa cứng, chỉ archive hoặc tạo version mới.
- **Lý do:** Xóa nguồn sẽ làm mất khả năng giải thích và khiến giá cũ out-of-date không kiểm soát được.

## QĐ-05 — Export chỉ điền đơn giá, giữ công thức gốc

- **Quyết định:** Khi xuất workbook, chỉ ghi ô đơn giá vật tư/nhân công đã được áp dụng; không ghi đè quantity, amount, subtotal, total hoặc công thức có sẵn.
- **Lý do:** Workbook mẫu có logic tính riêng theo từng biểu mẫu; Excel là nguồn sự thật cho công thức.

## QĐ-06 — Port 3000 và bind mạng

- **Người quyết:** Người dùng dự án
- **Ngày:** 2026-09-07
- **Trạng thái:** Accepted
- **Quyết định:** HTTP server mặc định chạy tại `0.0.0.0:3000`; có thể override bằng `APP_HOST`/`APP_PORT`.
- **Lý do:** Cho phép truy cập từ máy khác trong LAN và public qua Cloudflare Tunnel.
- **Đánh đổi:** Bind toàn bộ interface làm tăng bề mặt truy cập; chỉ mở trong mạng tin cậy, dùng firewall/auth trước production.

## QĐ-07 — Chatbot trả artifact và trạng thái báo giá có cấu trúc

- **Người quyết:** Người dùng dự án
- **Ngày:** 2026-09-08
- **Trạng thái:** Accepted
- **Quyết định:** File do tool chatbot xuất phải được trả bằng metadata có cấu trúc để UI dựng nút tải cùng origin. Trạng thái báo giá phải dùng các nhóm trạng thái explicit; không được suy diễn số dòng lỗi bằng `total_items - auto_approved`.
- **Lý do:** Câu chữ Markdown do mô hình sinh không đảm bảo tạo link có thể bấm, còn tổng số dòng bao gồm cả dòng `IGNORED` không cần áp giá.
- **Đánh đổi:** API chatbot có thêm trường `downloads`; frontend phải kiểm tra chặt URL artifact trước khi hiển thị.

## QĐ-08 — Mệnh lệnh rõ không hỏi lại xác nhận

- **Người quyết:** Người dùng dự án
- **Ngày:** 2026-09-08
- **Trạng thái:** Accepted
- **Quyết định:** Chatbot coi mệnh lệnh trực tiếp cho đúng hành động, như “xuất Excel đi” hoặc “áp giá đi”, là xác nhận thực thi trong cùng lượt. Chỉ dùng preview/xác nhận bổ sung cho câu hỏi, đề xuất hoặc ngữ cảnh mơ hồ.
- **Lý do:** Hỏi lại sau khi người dùng đã ra lệnh làm gián đoạn workflow và khiến thao tác lặp.
- **Đánh đổi:** Bộ nhận diện ý định phải hẹp, theo từng tool; không được suy diễn đồng ý từ câu hỏi hoặc nội dung không liên quan.

## QĐ-07 — Chatbot có chế độ nhanh và workspace toàn màn hình

- **Người quyết:** Người dùng dự án
- **Ngày:** 2026-09-08
- **Trạng thái:** Accepted
- **Quyết định:** Giữ nút chatbot nổi để hỏi nhanh và thêm tab `Chatbot` ở điều hướng trái để mở giao diện toàn bộ vùng làm việc. Hai chế độ dùng chung một phiên hội thoại và file đính kèm.
- **Lý do:** Panel nổi phù hợp câu hỏi ngắn nhưng thiếu không gian khi đọc câu trả lời dài hoặc thao tác với workbook.
- **Đánh đổi:** UI phải quản lý việc di chuyển cùng một chat surface giữa hai container mà không khởi tạo lại state.

## Quy tắc thay đổi

1. Ghi quyết định trước khi sửa tầng dẫn xuất.
2. Nếu quyết định ảnh hưởng kỹ thuật, thêm ADR tương ứng.
3. Chạy test và kiểm tra truy vết trước khi commit.
