# Data requirements next

This list is generated from source-gap observations. It estimates potentially unlockable rows; it does not promise exact coverage.

- Priceable rows observed: **2388**
- Source-supported rows: **2273**
- Potential unlock rows: **115**

| Priority category | Current gap | Potential rows | Requested data |
| --- | --- | ---: | --- |
| Labor-only | MISSING_SOURCE_DATA | 91 | Bảng labor master đã điền đơn giá và ít nhất 2–3 báo giá dự án gần nhất. |
| External quotation | MISSING_SOURCE_DATA | 24 | Danh sách nhà cung cấp/đầu mối để yêu cầu báo giá ngoài catalog. |
| Cable | MATCH_FAILURE | 0 | Bảng giá cáp hạ thế/trung thế hiện hành, gồm mã, cấu tạo, đơn vị, list/net, VAT và ngày hiệu lực. |
| Wire | MATCH_FAILURE | 0 | Bảng giá dây điện/dây điều khiển và dây dân dụng, gồm tiết diện, vật liệu ruột, đơn vị và giá ex-VAT. |
| Conduit | MATCH_FAILURE | 0 | Bảng giá ống luồn dây (PVC/HDPE/ruột gà), phụ kiện và quy cách DN/đường kính. |
| Pipe | MATCH_FAILURE | 0 | Bảng giá ống kỹ thuật/PPR/HDPE và phụ kiện theo đường kính, tiêu chuẩn và đơn vị. |
| Cable tray | MATCH_FAILURE | 0 | Bảng giá thang/máng/khay cáp và phụ kiện theo kích thước, vật liệu, lớp mạ. |
| Lighting | MATCH_FAILURE | 0 | Bảng giá đèn và thiết bị chiếu sáng (hãng, công suất, kiểu lắp, IP, VAT). |
| Switch/socket | MATCH_FAILURE | 0 | Bảng giá công tắc, ổ cắm và thiết bị bảo vệ theo hãng, cực, dòng định mức. |
| MCB/MCCB/Protection | MATCH_FAILURE | 0 | Bảng giá Schneider/ABB/LS hoặc hãng đang dùng cho MCB/MCCB/RCCB/RCBO. |
| Panel | MATCH_FAILURE | 0 | BOM/tủ điện được duyệt và giá gia công, thiết bị trong tủ, phụ kiện và quy cách. |
| Transformer | NO_PRICE_FOUND | 0 | Bảng giá máy biến áp/thiết bị nguồn theo công suất, điện áp và ngày hiệu lực. |
| Earthing | MATCH_FAILURE | 0 | Bảng giá cọc/thanh/dây tiếp địa và vật tư chống sét theo vật liệu, kích thước. |
| Lightning protection | MATCH_FAILURE | 0 | Bảng giá kim thu sét, dây thoát sét, phụ kiện và hồ sơ kỹ thuật. |
| Supports/accessories | MATCH_FAILURE | 0 | Bảng giá giá đỡ, kẹp, phụ kiện, co nối và vật tư lắp đặt phụ. |
| Civil works | MATCH_FAILURE | 0 | Đơn giá nhân công/vật tư xây dựng phụ trợ theo khu vực và đơn vị tính. |
| Unknown | MATCH_FAILURE | 0 | Danh mục chuẩn hóa hoặc mapping category cho các mô tả chưa đủ thông tin. |

Recommended order: add current supplier net-price files first, then labor master plus 2–3 recent project quotations, then fill external quotation contacts for categories that are intentionally out of catalog.
