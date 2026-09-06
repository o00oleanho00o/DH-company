# BOQ row classification audit

- Workbook: `BOQ-HỆ THỐNG ĐIỆN TRẠI LƠN HẢI HÀ-DH290124.xlsx`
- Workbook type: `HISTORICAL_BOQ`
- Included parsed BOQ/PANEL rows: **2829**
- Priceable line items: **2388**
- Non-priceable/uncertain rows: **441**
- Parser `data` rows: **2473** (priceable 2388, non-priceable 85)
- Rows on excluded `OTHER` sheets: **20**

## Classification counts

| Class | Rows |
| --- | ---: |
| `PRICEABLE_LINE_ITEM` | 2388 |
| `SECTION` | 68 |
| `SUBSECTION` | 151 |
| `NOTE` | 0 |
| `SUBTOTAL` | 56 |
| `TOTAL` | 6 |
| `HEADER` | 6 |
| `NON_PRICEABLE_REFERENCE` | 0 |
| `UNKNOWN` | 154 |

## By sheet

| Sheet | Type | Rows | Priceable | Breakdown |
| --- | --- | ---: | ---: | --- |
| 01.CẤP NGUỒN TỔNG THỂ | HISTORICAL_BOQ | 244 | 212 | HEADER=1, PRICEABLE_LINE_ITEM=212, SECTION=1, SUBTOTAL=17, TOTAL=1, UNKNOWN=12 |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | HISTORICAL_BOQ | 79 | 61 | HEADER=1, PRICEABLE_LINE_ITEM=61, SECTION=7, SUBTOTAL=5, TOTAL=1, UNKNOWN=4 |
| 03.HỆ THỐNG TIẾP ĐỊA | HISTORICAL_BOQ | 268 | 146 | HEADER=1, PRICEABLE_LINE_ITEM=146, SUBTOTAL=24, TOTAL=1, UNKNOWN=96 |
| 04.HỆ THỐNG CHỐNG SÉT | HISTORICAL_BOQ | 28 | 20 | HEADER=1, PRICEABLE_LINE_ITEM=20, SECTION=1, SUBTOTAL=2, TOTAL=1, UNKNOWN=3 |
| 05.TỦ ĐIỆN | PANEL_BOM | 103 | 88 | HEADER=1, PRICEABLE_LINE_ITEM=88, SUBTOTAL=6, TOTAL=1, UNKNOWN=7 |
| 06.HỆ THỐNG CHIẾU SÁNG BÊN TRON | HISTORICAL_BOQ | 622 | 584 | HEADER=1, PRICEABLE_LINE_ITEM=584, SECTION=2, SUBTOTAL=2, TOTAL=1, UNKNOWN=32 |
| 05. TỦ ĐIỆN CHI TIẾT | PANEL_BOM | 1485 | 1277 | PRICEABLE_LINE_ITEM=1277, SECTION=57, SUBSECTION=151 |

## Sample rows

### `PRICEABLE_LINE_ITEM`

| Sheet | Row | Description | Unit | Qty | Reason |
| --- | ---: | --- | --- | ---: | --- |
| 01.CẤP NGUỒN TỔNG THỂ | 10 | Cáp CXV 12x1C-240mm2 từ máy phát điện tới ngăn ATS của MSB | m | 0.0 | item_signal_zero_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 11 | Cáp CXV 8x1C-185mm2 từ máy phát điện tới ngăn ATS của MSB-A.1 | m | 21.0 | item_signal_with_operational_fields |
| 01.CẤP NGUỒN TỔNG THỂ | 12 | Cáp CV 1C-120mm2 từ máy phát điện tới ngăn ATS của MSB-A.1 | m | 21.0 | item_signal_with_operational_fields |
| 01.CẤP NGUỒN TỔNG THỂ | 13 | Cáp CXV 8x1C-185mm2 từ máy phát điện tới ngăn ATS của MSB-A.2 | m | 27.0 | item_signal_with_operational_fields |
| 01.CẤP NGUỒN TỔNG THỂ | 14 | Cáp CV 1C-120mm2 từ máy phát điện tới ngăn ATS của MSB-A.2 | m | 27.0 | item_signal_with_operational_fields |
| 01.CẤP NGUỒN TỔNG THỂ | 15 | Cáp CXV 2x1C-300mm2 từ máy phát điện tới ngăn ATS của MSB | m | 0.0 | item_signal_zero_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 16 | Cáp CXV 11x1C-300mm² từ máy phát điện tới ngăn ATS của MSB-B.1 | m | 22.0 | item_signal_with_operational_fields |
| 01.CẤP NGUỒN TỔNG THỂ | 17 | Cáp CV 1C-120mm2 từ máy phát điện tới ngăn ATS của MSB-B.1 | m | 22.0 | item_signal_with_operational_fields |

### `SECTION`

| Sheet | Row | Description | Unit | Qty | Reason |
| --- | ---: | --- | --- | ---: | --- |
| 01.CẤP NGUỒN TỔNG THỂ | 68 | VẬT TƯ KHÁC |  |  | structural_description_without_pricing_fields |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 34 | CHUỒNG HEO THỊT |  |  | structural_description_without_pricing_fields |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 39 | CHUỒNG HEO CAI SỮA  |  |  | structural_description_without_pricing_fields |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 44 | CHUỒNG HEO MANG THAI |  |  | structural_description_without_pricing_fields |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 49 | CHUỒNG HEO NÁI ĐẺ |  |  | structural_description_without_pricing_fields |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 59 | CHUỒNG HEO NỌC |  |  | structural_description_without_pricing_fields |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 73 | Khu cổng chính |  |  | structural_description_without_pricing_fields |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 78 | Khu xuất bán |  |  | structural_description_without_pricing_fields |

### `SUBSECTION`

| Sheet | Row | Description | Unit | Qty | Reason |
| --- | ---: | --- | --- | ---: | --- |
| 05. TỦ ĐIỆN CHI TIẾT | 23 | Đầu ra |  |  | structural_heading |
| 05. TỦ ĐIỆN CHI TIẾT | 26 | Khoang tụ bù |  |  | structural_heading |
| 05. TỦ ĐIỆN CHI TIẾT | 60 | Khoang MSB-A.1 |  |  | structural_heading |
| 05. TỦ ĐIỆN CHI TIẾT | 90 | Khoang MSB-A.2 |  |  | structural_heading |
| 05. TỦ ĐIỆN CHI TIẾT | 118 | Đầu ra |  |  | structural_heading |
| 05. TỦ ĐIỆN CHI TIẾT | 120 | Khoang tụ bù |  |  | structural_heading |
| 05. TỦ ĐIỆN CHI TIẾT | 154 | Khoang MSB-B.1 |  |  | structural_heading |
| 05. TỦ ĐIỆN CHI TIẾT | 184 | Khoang MSB-B.2 |  |  | structural_heading |

### `SUBTOTAL`

| Sheet | Row | Description | Unit | Qty | Reason |
| --- | ---: | --- | --- | ---: | --- |
| 01.CẤP NGUỒN TỔNG THỂ | 9 | DÂY NGUỒN TỪ TRẠM BIẾN ÁP ĐẾN NHÀ MÁY PHÁT |  |  | amount_without_item_unit_or_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 45 | TỦ ĐIỆN PHÂN PHỐI CHÍNH MSB |  |  | amount_without_item_unit_or_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 77 | TỦ ĐIỆN PHÂN PHỐI MDB-1A |  |  | amount_without_item_unit_or_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 99 | TỦ ĐIỆN PHÂN PHỐI MDB-2A |  |  | amount_without_item_unit_or_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 111 | TỦ ĐIỆN PHÂN PHỐI MDB-3A |  |  | amount_without_item_unit_or_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 123 | TỦ ĐIỆN PHÂN PHỐI MDB-1B |  |  | amount_without_item_unit_or_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 145 | TỦ ĐIỆN PHÂN PHỐI MDB-2B |  |  | amount_without_item_unit_or_quantity |
| 01.CẤP NGUỒN TỔNG THỂ | 158 | TỦ ĐIỆN PHÂN PHỐI MDB-3B |  |  | amount_without_item_unit_or_quantity |

### `TOTAL`

| Sheet | Row | Description | Unit | Qty | Reason |
| --- | ---: | --- | --- | ---: | --- |
| 01.CẤP NGUỒN TỔNG THỂ | 246 |  |  |  | total_label:tong cong a+b |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 86 |  |  |  | total_label:tong cong a+b |
| 03.HỆ THỐNG TIẾP ĐỊA | 182 |  |  |  | total_label:tong cong a+b |
| 04.HỆ THỐNG CHỐNG SÉT | 33 |  |  |  | total_label:tong cong a+b |
| 05.TỦ ĐIỆN | 106 |  |  |  | total_label:tong cong a+b |
| 06.HỆ THỐNG CHIẾU SÁNG BÊN TRON | 628 |  |  |  | total_label:tong cong a+b |

### `HEADER`

| Sheet | Row | Description | Unit | Qty | Reason |
| --- | ---: | --- | --- | ---: | --- |
| 01.CẤP NGUỒN TỔNG THỂ | 8 |  |  |  | repeated_header_labels |
| 02.HỆ THỐNG CHIẾU SÁNG NGOÀI | 8 |  |  |  | repeated_header_labels |
| 03.HỆ THỐNG TIẾP ĐỊA | 8 |  |  |  | repeated_header_labels |
| 04.HỆ THỐNG CHỐNG SÉT | 8 |  |  |  | repeated_header_labels |
| 05.TỦ ĐIỆN | 8 |  |  |  | repeated_header_labels |
| 06.HỆ THỐNG CHIẾU SÁNG BÊN TRON | 8 |  |  |  | repeated_header_labels |

### `UNKNOWN`

| Sheet | Row | Description | Unit | Qty | Reason |
| --- | ---: | --- | --- | ---: | --- |
| 01.CẤP NGUỒN TỔNG THỂ | 46 | MSB-A |  |  | ambiguous_description_or_missing_operational_fields |
| 01.CẤP NGUỒN TỔNG THỂ | 56 | MSB-B |  |  | ambiguous_description_or_missing_operational_fields |
| 01.CẤP NGUỒN TỔNG THỂ | 221 | Ổ CẮM ĐƯỜNG LÙA HEO |  |  | ambiguous_description_or_missing_operational_fields |
| 01.CẤP NGUỒN TỔNG THỂ | 241 |  |  |  | blank_or_parser_section_without_description |
| 01.CẤP NGUỒN TỔNG THỂ | 242 |  |  |  | blank_row |
| 01.CẤP NGUỒN TỔNG THỂ | 243 |  |  |  | blank_row |
| 01.CẤP NGUỒN TỔNG THỂ | 244 |  |  |  | blank_row |
| 01.CẤP NGUỒN TỔNG THỂ | 247 |  |  |  | blank_row |

## Interpretation

The classification is an audit view over parsed rows; it does not remove rows from the leakage-safe benchmark. `PRICEABLE_LINE_ITEM` means the row has an item/work signal plus an operational unit, quantity or price. Zero or missing quantities are retained and flagged in the reason so parser/data-quality gaps remain visible.
