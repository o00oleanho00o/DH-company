# Phạm vi dự án DH-Baogia (DHBG1)

## 1. Mục tiêu

Xây dựng nền tảng web nội bộ để nhập workbook Excel M&E, tạo catalog vật tư/
nhân công có provenance, áp giá báo giá mới bằng matching có kiểm soát, cho
người dùng rà soát các dòng chưa chắc chắn và xuất lại workbook mà không phá
công thức mẫu.

## 2. Trong phạm vi

- Nhập và inspect `.xls`/`.xlsx`, nhận diện sheet/header/cột theo nội dung.
- Lưu raw workbook, raw cells và liên kết nguồn tới sheet/dòng.
- Chuẩn hóa mô tả, mã, đơn vị, số lượng, thuộc tính kỹ thuật và phân loại dòng.
- Catalog vật tư/nhân công, giá phiên bản hóa, archive/restore/reprocess nguồn.
- Matching vật tư và nhân công độc lập; chọn candidate cao nhất từ ngưỡng cấu hình (mặc định 90%).
- Kết hợp giá vật tư và nhân công từ các candidate khác nhau trong cùng dòng.
- Chính sách giá: NCC mới nhất sau chiết khấu, giá nội bộ đã duyệt mới nhất, trung vị 3 dự án gần nhất.
- AI semantic reranking tùy chọn, bounded và không được tạo giá/công thức.
- Không gian rà soát, correction, audit và provenance.
- Xuất Excel giữ nguyên workbook layout/formula; chỉ điền unit price được áp dụng và thêm `AI Audit`.
- REST API và UI tiếng Việt cho Kho dữ liệu, Danh mục & Giá, Tạo báo giá, Bàn rà soát và Chatbot.
- Chatbot có panel nổi để hỏi nhanh và workspace toàn màn hình từ điều hướng trái; hội thoại được giữ nguyên khi đổi chế độ.
- Chatbot trả artifact xuất Excel dưới dạng metadata cùng origin để UI cung cấp nút tải trực tiếp; trạng thái quotation hiển thị riêng dòng đã áp giá, dòng bỏ qua và dòng cần xử lý.
- Chạy HTTP tại `0.0.0.0:3000`, truy cập LAN hoặc Cloudflare Tunnel.

## 3. Ngoài phạm vi

- ERP/kế toán, mua hàng, quản lý tồn kho thực tế hoặc đồng bộ giá NCC trực tuyến.
- OCR/PDF/CAD takeoff và tự thiết kế tủ/bảng điện.
- AI tự phê duyệt, tự phát hành báo giá, tự tạo đơn giá hoặc thay đổi công thức.
- PostgreSQL/pgvector, queue phân tán, multi-tenant và RBAC production.
- Tự động mở hoặc điều khiển Excel desktop.

## 4. Người dùng và trách nhiệm

| Vai trò | Trách nhiệm |
|---|---|
| Người lập báo giá | Upload BOQ, chọn chính sách, chạy và xuất kết quả |
| Người quản lý dữ liệu | Kiểm tra workbook, catalog, archive/reprocess nguồn |
| Người duyệt | Rà soát candidate/giá, nhập correction và phê duyệt |
| AI provider | Chỉ rerank candidate trong ranh giới đã cấp |

## 5. Tiêu chí thành công

- Không có giá áp dụng thiếu provenance.
- Dòng có candidate phù hợp ≥90% được tự động xử lý; dòng thấp hơn hoặc mâu thuẫn đi vào review.
- Giá vật tư và nhân công có thể đến từ hai candidate khác nhau.
- Workbook export mở được, công thức và ô không phải đơn giá giữ nguyên.
- `pytest -q` xanh và server truy cập được từ máy khác qua port 3000.
